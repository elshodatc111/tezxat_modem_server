"""Local server web paneli: modemlarni boshqarish, navbat, sozlamalar.

Ishga tushirish:  uvicorn app.web:app --host 0.0.0.0 --port 8088
"""
from __future__ import annotations

import glob
from contextlib import asynccontextmanager
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, config, scanner
from . import settings_store as st
from .bootstrap import ensure_defaults
from .db import session_scope, utcnow
from .hosting import HostingClient, HostingError
from .models import Admin, Event, Modem, ModemCommand, Outbox
from .security import hash_password, verify_password

BASE = Path(__file__).resolve().parent
@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_defaults()
    yield


app = FastAPI(title="SMS Modem Server", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=config.SECRET_KEY, same_site="lax",
    https_only=config.SESSION_HTTPS_ONLY, max_age=60 * 60 * 12, session_cookie="smsmodem",
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
TZ = ZoneInfo(config.TIMEZONE)


def localtime(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    if not dt:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ).strftime(fmt)


def ago(dt: datetime | None) -> str:
    if not dt:
        return "—"
    sec = int((utcnow() - dt).total_seconds())
    if sec < 60:
        return f"{sec} soniya oldin"
    if sec < 3600:
        return f"{sec // 60} daqiqa oldin"
    if sec < 86400:
        return f"{sec // 3600} soat oldin"
    return f"{sec // 86400} kun oldin"


templates.env.filters["localtime"] = localtime
templates.env.filters["ago"] = ago
templates.env.globals["version"] = __version__


# ----------------------------------------------------------------------------- auth
class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def admin_required(request: Request) -> str:
    user = request.session.get("admin")
    if not user:
        raise LoginRequired()
    return user


async def admin_post(request: Request) -> str:
    """POST so'rovlar: tizimga kirgan admin + CSRF tekshiruvi."""
    user = admin_required(request)
    form = await request.form()
    token = form.get("csrf") or request.headers.get("x-csrf")
    if not token or not secrets.compare_digest(str(token), request.session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="CSRF token noto'g'ri. Sahifani yangilang.")
    return user


def render(request: Request, name: str, **ctx):
    ctx.update(request=request, csrf=csrf_token(request), user=request.session.get("admin"), flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"text": text, "kind": kind}


_attempts: dict[str, list[float]] = {}


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    now = time.time()
    hist = [t for t in _attempts.get(ip, []) if now - t < 300]
    if len(hist) >= 8:
        flash(request, "Juda ko'p urinish. 5 daqiqadan keyin qayta urinib ko'ring.", "bad")
        return RedirectResponse("/login", status_code=303)
    with session_scope() as s:
        admin = s.query(Admin).filter(Admin.username == username).first()
        ok = bool(admin and verify_password(password, admin.password_hash))
    if not ok:
        hist.append(now)
        _attempts[ip] = hist
        flash(request, "Login yoki parol noto'g'ri", "bad")
        return RedirectResponse("/login", status_code=303)
    _attempts.pop(ip, None)
    request.session.clear()
    request.session["admin"] = username
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, _: str = Depends(admin_post)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ----------------------------------------------------------------------------- dashboard
def modem_view(m: Modem) -> dict:
    online = m.status in ("online", "busy") and m.last_seen is not None and (utcnow() - m.last_seen).total_seconds() < 120
    return {"m": m, "alive": online}


@app.get("/")
def dashboard(request: Request, _: str = Depends(admin_required)):
    with session_scope() as s:
        modems = [modem_view(m) for m in s.query(Modem).order_by(Modem.id)]
        counts = dict(s.query(Outbox.status, func.count()).group_by(Outbox.status).all())
        cfg = st.get_all(s)
        events = s.query(Event).order_by(Event.id.desc()).limit(8).all()
    last_ok = cfg.get("hosting_last_ok")
    hosting = {
        "configured": bool(cfg["hosting_url"] and cfg["gateway_token"]),
        "url": cfg["hosting_url"],
        "error": cfg["hosting_last_error"],
        "last_ok": datetime.fromisoformat(last_ok) if last_ok else None,
    }
    return render(request, "dashboard.html", modems=modems, counts=counts, hosting=hosting, events=events)


# ----------------------------------------------------------------------------- modemlar
def detect_ports() -> list[dict]:
    seen, out = set(), []
    for p in sorted(glob.glob("/dev/serial/by-path/*")):
        out.append({"device": p, "desc": "USB port bo'yicha (barqaror, tavsiya etiladi)"})
        seen.add(p)
    for p in sorted(glob.glob("/dev/serial/by-id/*")):
        out.append({"device": p, "desc": "by-id"})
        seen.add(p)
    try:
        from serial.tools import list_ports

        for p in list_ports.comports():
            if p.device not in seen:
                out.append({"device": p.device, "desc": p.description or ""})
    except Exception:
        pass
    for p in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        if p not in seen and not any(o["device"] == p for o in out):
            out.append({"device": p, "desc": ""})
    return out


@app.get("/modems")
def modems_page(request: Request, _: str = Depends(admin_required)):
    with session_scope() as s:
        modems = [modem_view(m) for m in s.query(Modem).order_by(Modem.id)]
    return render(request, "modems.html", modems=modems, ports=detect_ports())


@app.get("/api/ports")
def api_ports(_: str = Depends(admin_required)):
    return detect_ports()


def _modem_fields(name, port, baudrate, pin, daily_limit, min_interval, ussd_balance_code, ussd_package_code, auto_ussd_minutes, note):
    return dict(
        name=name.strip()[:60], port=port.strip()[:200], baudrate=baudrate, pin=(pin or "").strip() or None,
        daily_limit=max(0, daily_limit), min_interval=max(0.0, min_interval),
        ussd_balance_code=(ussd_balance_code or "").strip() or None, ussd_package_code=(ussd_package_code or "").strip() or None,
        auto_ussd_minutes=max(0, auto_ussd_minutes), note=(note or "").strip()[:255] or None,
    )


@app.post("/modems")
def modem_create(
    request: Request, _: str = Depends(admin_post),
    name: str = Form(...), port: str = Form(...), baudrate: int = Form(115200), pin: str = Form(""),
    daily_limit: int = Form(0), min_interval: float = Form(3.0), ussd_balance_code: str = Form(""),
    ussd_package_code: str = Form(""), auto_ussd_minutes: int = Form(0), note: str = Form(""),
):
    with session_scope() as s:
        if s.query(Modem).filter(Modem.name == name.strip()).first():
            flash(request, "Bunday nomli modem allaqachon bor", "bad")
            return RedirectResponse("/modems", status_code=303)
        m = Modem(**_modem_fields(name, port, baudrate, pin, daily_limit, min_interval, ussd_balance_code, ussd_package_code, auto_ussd_minutes, note))
        s.add(m)
        s.flush()
        mid = m.id
    flash(request, "Modem qo'shildi. Bir necha soniyada ulanadi.")
    return RedirectResponse(f"/modems/{mid}", status_code=303)


@app.get("/modems/{mid}")
def modem_page(mid: int, request: Request, _: str = Depends(admin_required)):
    with session_scope() as s:
        m = s.get(Modem, mid)
        if not m:
            raise HTTPException(404)
        cmds = s.query(ModemCommand).filter(ModemCommand.modem_id == mid).order_by(ModemCommand.id.desc()).limit(10).all()
        view = modem_view(m)
    return render(request, "modem.html", v=view, m=view["m"], cmds=cmds, ports=detect_ports())


def _age_seconds(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        return (utcnow() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return 1e9


@app.post("/api/scan")
def api_scan_start(_: str = Depends(admin_post)):
    """Modemlarni qidirishni boshlaydi (ishni worker bajaradi)."""
    state = scanner.read_state()
    if state["status"] in ("queued", "running") and _age_seconds(state["requested_at"]) < 120:
        return {"ok": True, "status": state["status"]}
    scanner.request_scan()
    return {"ok": True, "status": "queued"}


@app.get("/api/scan")
def api_scan_state(_: str = Depends(admin_required)):
    state = scanner.read_state()
    # so'rov 15 soniyadan beri "queued" turibdi: worker ishlamayapti
    state["worker_silent"] = state["status"] == "queued" and _age_seconds(state["requested_at"]) > 15
    if state["status"] in ("queued", "running") and _age_seconds(state["requested_at"]) > 180:
        state["status"] = "error"
        state["error"] = "Qidiruv vaqti tugadi. sms-modem-worker xizmatini tekshiring."
    return state


@app.post("/modems/quick-add")
def modem_quick_add(
    request: Request, name: str = Form(...), port: str = Form(...), ussd_balance_code: str = Form("*100#"),
    _: str = Depends(admin_post),
):
    """Qidiruv natijasidan bitta tugma bilan modem qo'shish."""
    name, port = name.strip()[:60], port.strip()[:200]
    if not name or not (port.startswith("/dev/") or port.startswith("mock://")):
        flash(request, "Nom va port to'g'ri kiritilmagan", "bad")
        return RedirectResponse("/modems", status_code=303)
    with session_scope() as s:
        if s.query(Modem).filter(Modem.name == name).first():
            flash(request, "Bunday nomli modem allaqachon bor. Boshqa nom bering.", "bad")
            return RedirectResponse("/modems", status_code=303)
        if s.query(Modem).filter(Modem.port == port).first():
            flash(request, "Bu port bilan modem allaqachon qo'shilgan", "bad")
            return RedirectResponse("/modems", status_code=303)
        m = Modem(name=name, port=port, ussd_balance_code=(ussd_balance_code or "").strip()[:40] or None)
        s.add(m)
        s.flush()
        mid = m.id
        st.log(s, "info", "web", f"Modem qo'shildi: {name} ({port})")
    flash(request, f"«{name}» qo'shildi. Bir necha soniyada ulanadi.")
    return RedirectResponse(f"/modems/{mid}", status_code=303)


@app.post("/modems/{mid}/set-port")
def modem_set_port(mid: int, request: Request, port: str = Form(...), _: str = Depends(admin_post)):
    port = port.strip()[:200]
    if not port.startswith("/dev/"):
        raise HTTPException(400, "Port noto'g'ri")
    with session_scope() as s:
        m = s.get(Modem, mid)
        if not m:
            raise HTTPException(404)
        m.port = port
        st.log(s, "info", "web", f"{m.name}: port yangilandi ({port})")
    flash(request, "Port yangilandi. Modem qayta ulanadi.")
    return RedirectResponse(f"/modems/{mid}", status_code=303)


@app.post("/modems/{mid}")
def modem_update(
    mid: int, request: Request, _: str = Depends(admin_post),
    name: str = Form(...), port: str = Form(...), baudrate: int = Form(115200), pin: str = Form(""),
    daily_limit: int = Form(0), min_interval: float = Form(3.0), ussd_balance_code: str = Form(""),
    ussd_package_code: str = Form(""), auto_ussd_minutes: int = Form(0), note: str = Form(""),
):
    with session_scope() as s:
        m = s.get(Modem, mid)
        if not m:
            raise HTTPException(404)
        dup = s.query(Modem).filter(Modem.name == name.strip(), Modem.id != mid).first()
        if dup:
            flash(request, "Bunday nomli modem allaqachon bor", "bad")
            return RedirectResponse(f"/modems/{mid}", status_code=303)
        for k, v in _modem_fields(name, port, baudrate, pin, daily_limit, min_interval, ussd_balance_code, ussd_package_code, auto_ussd_minutes, note).items():
            setattr(m, k, v)
    flash(request, "Saqlandi. Port o'zgargan bo'lsa, modem qayta ulanadi.")
    return RedirectResponse(f"/modems/{mid}", status_code=303)


@app.post("/modems/{mid}/toggle")
def modem_toggle(mid: int, request: Request, _: str = Depends(admin_post)):
    with session_scope() as s:
        m = s.get(Modem, mid)
        if not m:
            raise HTTPException(404)
        m.enabled = not m.enabled
        if not m.enabled:
            m.status = "disabled"
    return RedirectResponse(request.headers.get("referer") or f"/modems/{mid}", status_code=303)


@app.post("/modems/{mid}/delete")
def modem_delete(mid: int, request: Request, _: str = Depends(admin_post)):
    with session_scope() as s:
        m = s.get(Modem, mid)
        if m:
            s.query(Outbox).filter(Outbox.modem_id == mid, Outbox.status == "sending").update({"status": "pending"})
            s.delete(m)
    flash(request, "Modem o'chirildi")
    return RedirectResponse("/modems", status_code=303)


@app.post("/modems/{mid}/command")
def modem_command(mid: int, request: Request, kind: str = Form(...), payload: str = Form(""), _: str = Depends(admin_post)):
    if kind not in ("ussd", "ussd_balance", "ussd_package", "send_test", "reinit", "info"):
        raise HTTPException(400, "Noma'lum buyruq")
    if kind == "ussd" and not payload.strip():
        raise HTTPException(400, "USSD kodi kerak")
    with session_scope() as s:
        m = s.get(Modem, mid)
        if not m:
            raise HTTPException(404)
        if not m.enabled:
            return JSONResponse({"error": "Modem o'chirilgan"}, status_code=409)
        c = ModemCommand(modem_id=mid, kind=kind, payload=payload.strip() or None)
        s.add(c)
        s.flush()
        cid = c.id
    return {"id": cid}


@app.get("/api/commands/{cid}")
def command_status(cid: int, _: str = Depends(admin_required)):
    with session_scope() as s:
        c = s.get(ModemCommand, cid)
        if not c:
            raise HTTPException(404)
        return {"id": c.id, "status": c.status, "result": c.result}


@app.get("/api/state")
def api_state(_: str = Depends(admin_required)):
    with session_scope() as s:
        counts = dict(s.query(Outbox.status, func.count()).group_by(Outbox.status).all())
        modems = [{"id": m.id, "name": m.name, "status": m.status, "signal": m.signal} for m in s.query(Modem)]
    return {"counts": counts, "modems": modems}


# ----------------------------------------------------------------------------- navbat
@app.get("/outbox")
def outbox_page(request: Request, status: str = "", page: int = 1, _: str = Depends(admin_required)):
    per = 40
    with session_scope() as s:
        q = s.query(Outbox)
        if status in ("pending", "sending", "sent", "delivered", "failed"):
            q = q.filter(Outbox.status == status)
        total = q.count()
        rows = q.order_by(Outbox.id.desc()).offset((max(page, 1) - 1) * per).limit(per).all()
        names = {m.id: m.name for m in s.query(Modem)}
    return render(request, "outbox.html", rows=rows, names=names, status=status, page=page, pages=max(1, (total + per - 1) // per), total=total)


@app.post("/outbox/{oid}/retry")
def outbox_retry(oid: int, request: Request, _: str = Depends(admin_post)):
    with session_scope() as s:
        o = s.get(Outbox, oid)
        if o and o.status in ("failed", "sending"):
            o.status, o.attempts, o.error, o.next_try_at, o.reported_status = "pending", 0, None, None, ""
    return RedirectResponse(request.headers.get("referer") or "/outbox", status_code=303)


@app.post("/outbox/{oid}/fail")
def outbox_fail(oid: int, request: Request, _: str = Depends(admin_post)):
    """Navbatdagi SMS ni bekor qilish: hostingga 'failed' deb yuboriladi (pul foydalanuvchiga qaytariladi)."""
    with session_scope() as s:
        o = s.get(Outbox, oid)
        if o and o.status == "pending":
            o.status, o.error = "failed", "Admin tomonidan bekor qilindi"
    return RedirectResponse(request.headers.get("referer") or "/outbox", status_code=303)


# ----------------------------------------------------------------------------- sozlamalar
SETTING_KEYS = ["hosting_url", "poll_interval", "long_poll_wait", "pull_batch", "local_queue_cap", "heartbeat_interval",
                "max_send_attempts", "keep_days", "worker_name", "http_timeout",
                "min_balance", "balance_check_every", "balance_check_minutes"]


@app.get("/settings")
def settings_page(request: Request, _: str = Depends(admin_required)):
    with session_scope() as s:
        cfg = st.get_all(s)
    token = cfg["gateway_token"]
    return render(request, "settings.html", cfg=cfg, token_hint=("…" + token[-4:]) if token else "")


@app.post("/settings")
async def settings_save(request: Request, _: str = Depends(admin_post)):
    form = await request.form()
    with session_scope() as s:
        for k in SETTING_KEYS:
            v = str(form.get(k, "")).strip()
            if k == "hosting_url":
                v = v.rstrip("/")
                if v and not v.startswith(("http://", "https://")):
                    flash(request, "Hosting URL http:// yoki https:// bilan boshlanishi kerak", "bad")
                    return RedirectResponse("/settings", status_code=303)
            if v != "" or k == "hosting_url":
                st.set_(s, k, v)
        st.set_(s, "request_delivery_report", "1" if form.get("request_delivery_report") else "0")
        token = str(form.get("gateway_token", "")).strip()
        if token:                                  # bo'sh qoldirilsa, eski token saqlanadi
            st.set_(s, "gateway_token", token)
        st.log(s, "info", "web", "Sozlamalar o'zgartirildi")
    flash(request, "Sozlamalar saqlandi. Worker yangi sozlamani bir necha soniyada qo'llaydi.")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/test")
def settings_test(request: Request, hosting_url: str = Form(""), gateway_token: str = Form(""), _: str = Depends(admin_post)):
    with session_scope() as s:
        url = hosting_url.strip().rstrip("/") or st.get(s, "hosting_url")
        token = gateway_token.strip() or st.get(s, "gateway_token")
    if not url or not token:
        return {"ok": False, "message": "Hosting URL va token kiritilmagan"}
    client = HostingClient(url, token, timeout=15)
    try:
        client.ping()
        return {"ok": True, "message": "Ulanish muvaffaqiyatli ✔"}
    except HostingError as e:
        return {"ok": False, "message": str(e)}
    finally:
        client.close()


@app.post("/settings/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), _: str = Depends(admin_post)):
    if len(new) < 8:
        flash(request, "Yangi parol kamida 8 belgi bo'lsin", "bad")
        return RedirectResponse("/settings", status_code=303)
    with session_scope() as s:
        admin = s.query(Admin).filter(Admin.username == request.session["admin"]).first()
        if not admin or not verify_password(current, admin.password_hash):
            flash(request, "Joriy parol noto'g'ri", "bad")
            return RedirectResponse("/settings", status_code=303)
        admin.password_hash = hash_password(new)
    flash(request, "Parol o'zgartirildi")
    return RedirectResponse("/settings", status_code=303)


# ----------------------------------------------------------------------------- jurnal
@app.get("/events")
def events_page(request: Request, level: str = "", _: str = Depends(admin_required)):
    with session_scope() as s:
        q = s.query(Event)
        if level in ("info", "warn", "error"):
            q = q.filter(Event.level == level)
        rows = q.order_by(Event.id.desc()).limit(200).all()
    return render(request, "events.html", rows=rows, level=level)
