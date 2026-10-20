"""Modem worker: modemlarni boshqaradi, hostingdan navbatni oladi va natijani qaytaradi.

Ishga tushirish:  python -m app.worker
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from . import __version__, config, pdu as pdumod
from . import settings_store as st
from .balance import parse_balance
from .bootstrap import ensure_defaults
from .db import session_scope, utcnow
from .hosting import HostingClient, HostingError
from .models import Event, Modem, ModemCommand, Outbox, SmsRef
from .modem import ATModem, MockTransport, ModemError, SerialTransport
from .scanner import ScanWatcher

log = logging.getLogger("worker")

_MOCKS: dict[str, MockTransport] = {}


def make_transport(port: str, baudrate: int):
    if port.startswith("mock://"):
        t = _MOCKS.get(port)
        if t is None or t.closed:
            t = MockTransport(name=port, imei=f"86000000000{abs(hash(port)) % 10000:04d}")
            _MOCKS[port] = t
        t.closed = False
        return t
    return SerialTransport(port, baudrate)


def local_today():
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def add_event(level: str, source: str, message: str) -> None:
    try:
        with session_scope() as s:
            st.log(s, level, source, message)
    except Exception:
        log.exception("event yozib bo'lmadi")


def digits_tail(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())[-9:]


# ============================================================================ bitta modem
class ModemRunner(threading.Thread):
    """Bitta modem = bitta oqim. Modem bo'sh bo'lganda navbatdan keyingi SMS ni o'zi oladi
    (shu tarzda bir nechta modem avtomatik yuklanadi; hammasi band bo'lsa SMS lar navbatda kutadi)."""

    def __init__(self, modem_id: int, key: tuple, transport_factory=make_transport):
        super().__init__(daemon=True, name=f"modem-{modem_id}")
        self.modem_id = modem_id
        self.key = key
        self.stop_event = threading.Event()
        self.transport_factory = transport_factory
        self.last_send = 0.0
        self.last_status = 0.0
        self.last_cleanup = time.monotonic()
        self.sent_since_check = 0
        self.name_cache = f"#{modem_id}"

    def stop(self) -> None:
        self.stop_event.set()

    # ---- holat
    def _update(self, **fields) -> None:
        with session_scope() as s:
            m = s.get(Modem, self.modem_id)
            if m:
                for k, v in fields.items():
                    setattr(m, k, v)

    def run(self) -> None:
        # worker qayta ishga tushganda "running" qolib ketgan buyruqlarni yopamiz
        with session_scope() as s:
            for c in s.query(ModemCommand).filter(ModemCommand.modem_id == self.modem_id, ModemCommand.status == "running"):
                c.status, c.result, c.finished_at = "error", "Worker qayta ishga tushdi", utcnow()
            # "sending" holatida qolib ketgan SMS lar: qayta navbatga
            for o in s.query(Outbox).filter(Outbox.modem_id == self.modem_id, Outbox.status == "sending"):
                o.status = "pending"
        while not self.stop_event.is_set():
            try:
                self._session()
            except Exception as e:  # kutilmagan xato — oqim o'lib qolmasin
                log.exception("modem oqimi xatosi")
                self._update(status="error", last_error=f"{e.__class__.__name__}: {e}"[:300])
                self.stop_event.wait(10)
        self._update(status="offline")

    def _session(self) -> None:
        with session_scope() as s:
            m = s.get(Modem, self.modem_id)
            if m is None:
                self.stop_event.set()
                return
            port, baud, pin, self.name_cache, enabled = m.port, m.baudrate, m.pin, m.name, m.enabled
        if not enabled:
            self._update(status="disabled")
            self.stop_event.wait(5)
            return

        at: ATModem | None = None
        try:
            at = ATModem(self.transport_factory(port, baud))
            at.init(pin)
        except ModemError as e:
            if at:
                at.close()
            self._update(status="offline", last_error=str(e)[:300], signal=None)
            self.stop_event.wait(10)
            return

        add_event("info", self.name_cache, f"Modem ulandi ({at.model or '?'}, IMEI {at.imei or '?'})")
        self._update(status="online", last_error=None, model=(at.model or "")[:80], imei=at.imei)
        self.last_status = 0
        try:
            self._loop(at)
        except ModemError as e:
            add_event("error", self.name_cache, f"Modem aloqasi uzildi: {e}")
            self._update(status="offline", last_error=str(e)[:300], signal=None)
            self.stop_event.wait(5)
        finally:
            at.close()

    # ---- asosiy sikl
    def _loop(self, at: ATModem) -> None:
        while not self.stop_event.is_set():
            worked = self._handle_commands(at)
            at.poll()
            self._apply_reports(at.take_reports())

            now = time.monotonic()
            if now - self.last_status > 30:
                self._refresh_status(at)
                self.last_status = now
                self._auto_ussd(at)
            if now - self.last_cleanup > 3600:
                at.cleanup_memory()
                self.last_cleanup = now

            if self._try_send(at):
                worked = True
            if self.sent_since_check and self._funds_check_due():
                self.sent_since_check = 0
                self._auto_ussd(at, force=True, only_balance=True)
            if not worked:
                self.stop_event.wait(0.4)

    def _refresh_status(self, at: ATModem) -> None:
        sim = at.sim_state()
        sig = at.signal()
        op = at.operator()
        with session_scope() as s:
            m = s.get(Modem, self.modem_id)
            if not m:
                return
            m.sim_state, m.signal, m.operator, m.last_seen = sim, sig, op, utcnow()
            if m.status in ("offline", "error"):
                m.status = "online"
            if not m.enabled:
                raise ModemError("Modem o'chirildi")

    def _funds_check_due(self) -> bool:
        with session_scope() as s:
            if st.get_int(s, "min_balance", 0) <= 0:
                return False
            return self.sent_since_check >= max(1, st.get_int(s, "balance_check_every", 5))

    def _auto_ussd(self, at: ATModem, force: bool = False, only_balance: bool = False) -> None:
        with session_scope() as s:
            m = s.get(Modem, self.modem_id)
            if not m:
                return
            min_bal = st.get_int(s, "min_balance", 0)
            interval = m.auto_ussd_minutes
            if interval <= 0 and min_bal > 0:
                interval = max(1, st.get_int(s, "balance_check_minutes", 10))
            if interval <= 0 and not force:
                return
            due = force or m.balance_at is None or (utcnow() - m.balance_at) > timedelta(minutes=interval)
            codes = (m.ussd_balance_code, "" if only_balance else m.ussd_package_code)
        if not due:
            return
        for kind, code in zip(("balance", "package"), codes):
            if code:
                try:
                    self._store_ussd(kind, at.ussd(code))
                except ModemError as e:
                    if e.fatal:
                        raise
                    self._store_ussd(kind, f"Xato: {e}")
        self._check_funds()

    def _check_funds(self) -> None:
        """SIM balansi minimaldan past bo'lsa modemni "no_funds" qiladi va kutayotgan SMS larni xato bilan yopadi
        (hostingda pul foydalanuvchiga qaytariladi). Balans tiklansa, modem yana ishga tushadi."""
        with session_scope() as s:
            m = s.get(Modem, self.modem_id)
            if not m:
                return
            min_bal = st.get_int(s, "min_balance", 0)
            if min_bal <= 0:
                if m.status == "no_funds":
                    m.status = "online"
                return
            value = parse_balance(m.balance_text)
            if value is None:
                return
            if value < min_bal:
                if m.status != "no_funds":
                    m.status = "no_funds"
                    m.last_error = f"SIM balansi past: {value:g} < {min_bal} so'm"
                    st.log(s, "error", self.name_cache, f"SIM puli yetarli emas ({value:g} so'm). SMS yuborish to'xtatildi")
                    n = 0
                    for o in s.query(Outbox).filter(Outbox.status == "pending"):
                        o.status, o.error = "failed", "SIM kartada mablag' yetarli emas"
                        n += 1
                    if n:
                        st.log(s, "warn", self.name_cache, f"{n} ta kutayotgan SMS bekor qilindi (pul qaytariladi)")
            elif m.status == "no_funds":
                m.status, m.last_error = "online", None
                st.log(s, "info", self.name_cache, f"SIM puli tiklandi ({value:g} so'm). Modem ishga tushdi")

    def _store_ussd(self, kind: str, text: str) -> None:
        with session_scope() as s:
            m = s.get(Modem, self.modem_id)
            if m:
                if kind == "balance":
                    m.balance_text, m.balance_at = text, utcnow()
                else:
                    m.package_text, m.package_at = text, utcnow()
        if kind == "balance":
            self._check_funds()

    # ---- buyruqlar (web paneldan)
    def _handle_commands(self, at: ATModem) -> bool:
        with session_scope() as s:
            cmd = s.query(ModemCommand).filter(ModemCommand.modem_id == self.modem_id, ModemCommand.status == "pending").order_by(ModemCommand.id).first()
            if not cmd:
                return False
            cmd.status = "running"
            cid, kind, payload = cmd.id, cmd.kind, cmd.payload or ""
            m = s.get(Modem, self.modem_id)
            codes = {"ussd_balance": m.ussd_balance_code, "ussd_package": m.ussd_package_code}

        status, result, fatal = "done", "", None
        try:
            if kind == "ussd":
                result = at.ussd(payload.strip())
            elif kind in ("ussd_balance", "ussd_package"):
                code = (payload or codes.get(kind) or "").strip()
                if not code:
                    raise ModemError("USSD kodi kiritilmagan (modem sozlamalarida belgilang)")
                result = at.ussd(code)
                self._store_ussd("balance" if kind == "ussd_balance" else "package", result)
            elif kind == "send_test":
                data = json.loads(payload)
                self._update(status="busy")
                mrs = at.send_sms(data["to"], data.get("text") or "Test SMS", request_report=False)
                result = f"Yuborildi (MR={','.join(map(str, mrs))})"
            elif kind == "info":
                self._refresh_status(at)
                result = f"Model: {at.model}\nIMEI: {at.imei}\nSIM: {at.sim_state()}\nOperator: {at.operator()}\nSignal(CSQ): {at.signal()}"
            elif kind == "reinit":
                result = "Modem qayta ulanmoqda..."
                fatal = ModemError("Qayta ishga tushirish so'raldi", fatal=True)
            else:
                raise ModemError(f"Noma'lum buyruq: {kind}")
        except ModemError as e:
            status, result = "error", str(e)
            if e.fatal:
                fatal = e
        except Exception as e:
            status, result = "error", f"{e.__class__.__name__}: {e}"

        with session_scope() as s:
            c = s.get(ModemCommand, cid)
            c.status, c.result, c.finished_at = status, result[:4000], utcnow()
        if kind == "send_test":
            self._update(status="online")
        if fatal:
            raise fatal
        return True

    # ---- yuborish
    def _claim(self, s) -> Outbox | None:
        q = (
            select(Outbox)
            .where(Outbox.status == "pending", or_(Outbox.next_try_at.is_(None), Outbox.next_try_at <= utcnow()))
            .order_by(Outbox.id)
            .limit(1)
        )
        if config.DB_SKIP_LOCKED:
            q = q.with_for_update(skip_locked=True)
        else:
            q = q.with_for_update()
        row = s.execute(q).scalar_one_or_none()
        if row is not None:
            row.status = "sending"
            row.modem_id = self.modem_id
            row.attempts += 1
        return row

    def _try_send(self, at: ATModem) -> bool:
        with session_scope() as s:
            m = s.get(Modem, self.modem_id)
            if not m or not m.enabled:
                raise ModemError("Modem o'chirildi yoki o'chirib tashlandi")
            today = local_today()
            if m.today != today:
                m.today, m.sent_today = today, 0
            if m.status == "no_funds":
                return False
            if m.daily_limit and m.sent_today >= m.daily_limit:
                return False
            if time.monotonic() - self.last_send < (m.min_interval or 0):
                return False
            report = st.get(s, "request_delivery_report") == "1"
            row = self._claim(s)
            if row is None:
                return False
            oid, phone, text, attempts = row.id, row.phone, row.text, row.attempts
            m.status = "busy"

        try:
            mrs = at.send_sms(phone, text, request_report=report)
        except ModemError as e:
            self.last_send = time.monotonic()
            self._on_fail(oid, attempts, str(e))
            self.sent_since_check = 10**6        # xatodan keyin balansni darhol tekshiramiz
            if e.fatal:
                raise
            self._update(status="online")
            return True
        except Exception as e:      # masalan noto'g'ri raqam (ValueError)
            self.last_send = time.monotonic()
            self._on_fail(oid, 99, f"{e.__class__.__name__}: {e}")
            self._update(status="online")
            return True

        self.last_send = time.monotonic()
        with session_scope() as s:
            o = s.get(Outbox, oid)
            o.status, o.error, o.sent_at = "sent", None, utcnow()
            for i, mr in enumerate(mrs, start=1):
                s.add(SmsRef(outbox_id=oid, modem_id=self.modem_id, mr=mr, part=i, total=len(mrs)))
            m = s.get(Modem, self.modem_id)
            m.sent_today += 1
            m.sent_total += 1
            m.status = "online"
            m.last_error = None
        self.sent_since_check += 1
        return True

    def _on_fail(self, oid: int, attempts: int, error: str) -> None:
        with session_scope() as s:
            max_attempts = st.get_int(s, "max_send_attempts", 3)
            o = s.get(Outbox, oid)
            o.error = error[:300]
            if attempts >= max_attempts:
                o.status = "failed"
            else:
                o.status = "pending"
                o.next_try_at = utcnow() + timedelta(seconds=5 * attempts)
            m = s.get(Modem, self.modem_id)
            m.last_error = error[:300]
            if o.status == "failed":
                m.failed_total += 1
            st.log(s, "error" if o.status == "failed" else "warn", self.name_cache, f"SMS #{o.remote_id}: {error}")

    # ---- yetkazish hisobotlari
    def _apply_reports(self, reports: list[pdumod.StatusReport]) -> None:
        if not reports:
            return
        with session_scope() as s:
            for r in reports:
                ref = s.execute(
                    select(SmsRef).where(SmsRef.modem_id == self.modem_id, SmsRef.mr == r.mr, SmsRef.delivered.is_(False))
                    .order_by(SmsRef.id.desc()).limit(1)
                ).scalar_one_or_none()
                if ref is None:
                    continue
                o = s.get(Outbox, ref.outbox_id)
                if o is None or digits_tail(o.phone) != digits_tail(r.recipient):
                    continue
                if r.final and r.delivered:
                    ref.delivered = True
                    delivered = s.execute(select(func.count()).select_from(SmsRef).where(SmsRef.outbox_id == o.id, SmsRef.delivered.is_(True))).scalar_one()
                    if delivered >= ref.total and o.status == "sent":
                        o.status = "delivered"
                elif r.final:
                    st.log(s, "warn", self.name_cache, f"SMS #{o.remote_id}: operator yetkazilmadi dedi (ST=0x{r.status:02X})")
                    if o.status == "sent":
                        o.status, o.error = "failed", f"Operator yetkazmadi (ST=0x{r.status:02X})"
                        m = s.get(Modem, self.modem_id)
                        if m:
                            m.failed_total += 1


# ============================================================================ modemlar boshqaruvchisi
class ModemManager:
    def __init__(self, transport_factory=make_transport):
        self.runners: dict[int, ModemRunner] = {}
        self.transport_factory = transport_factory

    def tick(self) -> None:
        with session_scope() as s:
            modems = {m.id: (m.port, m.baudrate, m.pin, m.enabled) for m in s.query(Modem)}
        for mid, runner in list(self.runners.items()):
            wanted = modems.get(mid)
            changed = wanted is None or (wanted[0], wanted[1], wanted[2]) != runner.key
            if changed or not runner.is_alive():
                runner.stop()
                runner.join(timeout=15)
                del self.runners[mid]
        for mid, (port, baud, pin, enabled) in modems.items():
            if mid not in self.runners:
                r = ModemRunner(mid, (port, baud, pin), self.transport_factory)
                self.runners[mid] = r
                r.start()

    def stop_all(self) -> None:
        for r in self.runners.values():
            r.stop()
        for r in self.runners.values():
            r.join(timeout=10)


# ============================================================================ hosting bilan sinxronizatsiya
class SyncRunner(threading.Thread):
    def __init__(self, client_factory=HostingClient):
        super().__init__(daemon=True, name="sync")
        self.stop_event = threading.Event()
        self.client_factory = client_factory
        self.last_heartbeat = 0.0
        self.last_housekeeping = 0.0
        self.failures = 0
        self.last_ok_write = 0.0
        self.last_error_text = ""

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        client: HostingClient | None = None
        client_cfg: tuple | None = None
        while not self.stop_event.is_set():
            delay = 3.0
            try:
                with session_scope() as s:
                    cfg_all = st.get_all(s)
                url, token = cfg_all["hosting_url"].strip(), cfg_all["gateway_token"].strip()
                delay = max(0.5, float(cfg_all["poll_interval"] or 3))
                if not url or not token:
                    self.stop_event.wait(5)
                    continue
                cfg = (url, token, cfg_all["http_timeout"])
                if client is None or cfg != client_cfg:      # hosting manzili o'zgargan bo'lsa qayta ulanamiz
                    if client:
                        client.close()
                    client = self.client_factory(url, token, float(cfg_all["http_timeout"] or 30))
                    client_cfg = cfg
                got = self.cycle(client, cfg_all)
                self._ok()
                self.failures = 0
                if got:
                    delay = 0.2     # navbat bor bo'lsa, kutmasdan yana so'raymiz
            except HostingError as e:
                self._fail(str(e))
                delay = min(30, 2 ** min(self.failures, 5))
            except Exception as e:
                log.exception("sync xatosi")
                self._fail(f"{e.__class__.__name__}: {e}")
                delay = 10
            self.stop_event.wait(delay)

    def _ok(self) -> None:
        # Bazaga har aylanishda yozmaymiz: xato tiklanganda yoki 10 soniyada bir marta
        recovered = self.failures > 0
        if not recovered and time.monotonic() - self.last_ok_write < 10:
            return
        self.last_ok_write = time.monotonic()
        with session_scope() as s:
            st.set_(s, "hosting_last_ok", utcnow().isoformat())
            if st.get(s, "hosting_last_error"):
                st.set_(s, "hosting_last_error", "")
                st.log(s, "info", "sync", "Hosting bilan aloqa tiklandi")
        self.last_error_text = ""

    def _fail(self, text: str) -> None:
        self.failures += 1
        with session_scope() as s:
            st.set_(s, "hosting_last_error", f"{utcnow().isoformat()} | {text}"[:500])
            if text != self.last_error_text:     # bir xil xatoni takror yozmaymiz
                st.log(s, "error", "sync", f"Hosting xatosi: {text}")
        self.last_error_text = text

    # --- bitta aylanish
    def cycle(self, client, cfg: dict) -> int:
        self.report_results(client)
        got = self.pull(client, cfg)
        now = time.monotonic()
        if now - self.last_heartbeat >= max(5, int(cfg["heartbeat_interval"] or 15)):
            client.heartbeat(self.heartbeat_payload(cfg))
            self.last_heartbeat = now
        if now - self.last_housekeeping > 3600:
            self.housekeeping(cfg)
            self.last_housekeeping = now
        return got

    def report_results(self, client) -> int:
        with session_scope() as s:
            rows = s.execute(
                select(Outbox, Modem.name)
                .join(Modem, Modem.id == Outbox.modem_id, isouter=True)
                .where(Outbox.status.in_(("sent", "delivered", "failed")), Outbox.reported_status != Outbox.status)
                .order_by(Outbox.id).limit(50)
            ).all()
            payload = [
                {"id": o.remote_id, "status": o.status, "error": o.error if o.status == "failed" else None, "modem": mname}
                for o, mname in rows
            ]
            sent_ids = {o.id: o.status for o, _ in rows}
        if not payload:
            return 0
        client.result(payload)       # xato bo'lsa — reported_status yangilanmaydi, keyingi aylanishda qayta uriniladi
        with session_scope() as s:
            for oid, status in sent_ids.items():
                o = s.get(Outbox, oid)
                if o:
                    o.reported_status = status
        return len(payload)

    def pull(self, client, cfg: dict) -> int:
        with session_scope() as s:
            pending = s.query(Outbox).filter(Outbox.status.in_(("pending", "sending"))).count()
            ready = s.query(Modem).filter(Modem.enabled.is_(True), Modem.status.in_(("online", "busy"))).count()
        cap = int(cfg["local_queue_cap"] or 30)
        if ready == 0 or pending >= cap:
            return 0     # modem yo'q yoki mahalliy navbat to'la: SMS lar hostingda "navbatda" turaveradi
        limit = max(1, min(int(cfg["pull_batch"] or 5), cap - pending, 20))
        messages = client.pull(limit, int(cfg["long_poll_wait"] or 0), cfg["worker_name"])
        if not messages:
            return 0
        with session_scope() as s:
            for m in messages:
                existing = s.query(Outbox).filter(Outbox.remote_id == m["id"]).first()
                if existing:
                    done = existing.status in ("sent", "delivered", "failed") and existing.reported_status == existing.status
                    if not done:
                        continue     # haqiqiy takror: SMS hali ishlanmoqda
                    # Eski, allaqachon yakunlangan yozuv: hosting ID lari qaytadan boshlangan (yangi baza/hosting).
                    s.delete(existing)
                    s.flush()
                info = pdumod.analyze(m["text"])
                s.add(Outbox(remote_id=m["id"], phone=m["to"], text=m["text"], encoding=info["encoding"], parts=info["parts"]))
        return len(messages)

    def heartbeat_payload(self, cfg: dict) -> dict:
        with session_scope() as s:
            modems = [
                {
                    "id": m.id, "name": m.name, "port": m.port, "status": m.status, "signal": m.signal,
                    "operator": m.operator, "sim": m.sim_state, "balance_text": (m.balance_text or "")[:200],
                    "package_text": (m.package_text or "")[:200], "sent_today": m.sent_today, "sent_total": m.sent_total,
                    "failed_total": m.failed_total, "last_error": m.last_error,
                }
                for m in s.query(Modem).order_by(Modem.id)
            ]
            queue = {
                "pending": s.query(Outbox).filter(Outbox.status == "pending").count(),
                "sending": s.query(Outbox).filter(Outbox.status == "sending").count(),
                "unreported": s.query(Outbox).filter(Outbox.status.in_(("sent", "delivered", "failed")), Outbox.reported_status != Outbox.status).count(),
            }
        return {"worker": cfg["worker_name"], "version": __version__, "modems": modems, "queue": queue}

    def housekeeping(self, cfg: dict) -> None:
        keep = timedelta(days=max(1, int(cfg["keep_days"] or 14)))
        with session_scope() as s:
            old = utcnow() - keep
            s.query(Outbox).filter(Outbox.status.in_(("sent", "delivered", "failed")), Outbox.reported_status == Outbox.status, Outbox.updated_at < old).delete(synchronize_session=False)
            s.query(SmsRef).filter(SmsRef.created_at < old).delete(synchronize_session=False)
            s.query(ModemCommand).filter(ModemCommand.created_at < utcnow() - timedelta(days=2)).delete(synchronize_session=False)
            s.query(Event).filter(Event.created_at < utcnow() - timedelta(days=30)).delete(synchronize_session=False)


# ============================================================================ main
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ensure_defaults()
    manager = ModemManager()
    sync = SyncRunner()
    scan_watcher = ScanWatcher()
    stop = threading.Event()

    def _stop(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    add_event("info", "worker", f"Worker ishga tushdi (v{__version__})")
    sync.start()
    scan_watcher.start()
    while not stop.is_set():
        try:
            manager.tick()
        except Exception:
            log.exception("manager xatosi")
        stop.wait(3)
    log.info("To'xtatilmoqda...")
    sync.stop()
    scan_watcher.stop()
    manager.stop_all()
    add_event("info", "worker", "Worker to'xtatildi")


if __name__ == "__main__":
    main()
