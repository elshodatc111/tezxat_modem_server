"""USB modemlarni avtomatik topish.

Ishlash tartibi:
  1) Web panel "Modemlarni qidirish" bosilganda bazaga `scan_request` yozadi.
  2) Worker (portlarning yagona egasi) ScanWatcher oqimi orqali so'rovni ko'radi, barcha
     /dev/serial/by-path/* portlarini USB qurilma bo'yicha guruhlaydi va har bir guruhda
     AT buyrug'iga OK deydigan portni topadi (USB hub orqali ulangan 10 ta modem ham).
  3) Natija `scan_result` (JSON) sifatida bazaga yoziladi; panel uni kartochkalar qilib ko'rsatadi:
     modelni, IMEI, operator va signalni ko'rsatadi, foydalanuvchi nom berib "Qo'shish" ni bosadi.

Band portlarga (qo'shilgan modemlar) tegmaymiz: ularni worker allaqachon ochib qo'ygan.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import settings_store as st
from .db import session_scope, utcnow
from .models import Modem

log = logging.getLogger("scanner")

# pci-0000:00:1d.0-usb-0:1.2:1.3-port0  ->  guruh: ...-usb-0:1.2   (qurilma), interfeys: 3
_BYPATH = re.compile(r"^(?P<group>.*-usb-\d+:[\d.]+):(?P<cfg>\d+)\.(?P<iface>\d+)-port\d+$")
_SYSFS_DEV = re.compile(r"^\d+-[\d.]+$")
_SYSFS_IFACE = re.compile(r"^(?P<dev>\d+-[\d.]+):(?P<cfg>\d+)\.(?P<iface>\d+)$")

OPERATOR_BALANCE_CODES = {"beeline": "*102#"}   # qolganlar uchun *100#


# ----------------------------------------------------------------------------- portlar ro'yxati
def _sysfs_info(tty: str) -> tuple[str, int]:
    """/dev/ttyUSB3 -> (USB qurilma nomi, interfeys raqami). Topilmasa: (tty nomi, 0)."""
    name = os.path.basename(tty)
    try:
        real = os.path.realpath(f"/sys/class/tty/{name}/device")
        parts = real.split(os.sep)
        group, iface = name, 0
        for p in reversed(parts):
            m = _SYSFS_IFACE.match(p)
            if m:
                iface = int(m.group("iface"))
            if _SYSFS_DEV.match(p):
                group = p
                break
        return group, iface
    except Exception:
        return name, 0


def list_serial_ports() -> list[dict]:
    """[{path, real, group, iface}] — by-path nomlari afzal (USB port o'zgarmasa barqaror)."""
    items: list[dict] = []
    seen_real: set[str] = set()
    for p in sorted(glob.glob("/dev/serial/by-path/*")):
        real = os.path.realpath(p)
        m = _BYPATH.match(os.path.basename(p))
        group = m.group("group") if m else os.path.basename(p)
        iface = int(m.group("iface")) if m else 0
        items.append({"path": p, "real": real, "group": group, "iface": iface})
        seen_real.add(real)
    for p in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        real = os.path.realpath(p)
        if real in seen_real:
            continue
        group, iface = _sysfs_info(p)
        items.append({"path": p, "real": real, "group": group, "iface": iface})
    return items


# ----------------------------------------------------------------------------- port tekshiruvi
def probe_port(path: str, baud: int = 115200) -> dict | None:
    """Portga AT yuboradi. OK desa — modem haqida ma'lumot, aks holda None."""
    from .modem import ATModem, ModemError, SerialTransport

    try:
        transport = SerialTransport(path, baud)
    except ModemError:
        return None
    at = ATModem(transport)
    try:
        alive = False
        for _ in range(2):
            try:
                transport.reset_input()
                at.cmd("AT", timeout=1.5)
                alive = True
                break
            except ModemError:
                continue
        if not alive:
            return None

        info: dict = {"manufacturer": None, "model": None, "imei": None, "sim": None, "signal": None, "operator": None}
        try:
            at.cmd("ATE0", timeout=2)
        except ModemError:
            pass
        for key, command in (("manufacturer", "AT+CGMI"), ("model", "AT+CGMM")):
            try:
                lines = at.cmd(command, 3)
                info[key] = (lines[0] if lines else None) or None
            except ModemError as e:
                if e.fatal:
                    return info
        try:
            lines = at.cmd("AT+CGSN", 3)
            info["imei"] = next((ln for ln in lines if re.fullmatch(r"\d{10,20}", ln)), None)
        except ModemError as e:
            if e.fatal:
                return info
        for key, fn in (("sim", at.sim_state), ("signal", at.signal), ("operator", at.operator)):
            try:
                info[key] = fn()
            except ModemError as e:
                if e.fatal:
                    break
        return info
    finally:
        at.close()


# ----------------------------------------------------------------------------- skan
def _next_name(taken: set[str]) -> str:
    n = 1
    while f"Modem-{n}" in taken:
        n += 1
    return f"Modem-{n}"


def scan(
    existing: list[dict],
    lister: Callable[[], list[dict]] = list_serial_ports,
    prober: Callable[[str], dict | None] = probe_port,
    max_workers: int = 6,
) -> list[dict]:
    """USB qurilmalarni guruhlab tekshiradi.

    existing: [{"id", "name", "port", "imei"}] — allaqachon qo'shilgan modemlar.
    Natija — har bir USB modem uchun bitta yozuv (status: new | added | moved | no_at).
    """
    ports = lister()
    groups: dict[str, list[dict]] = {}
    for p in ports:
        groups.setdefault(p["group"], []).append(p)

    used_real: dict[str, dict] = {}
    for m in existing:
        used_real[os.path.realpath(m["port"]) if not m["port"].startswith("mock://") else m["port"]] = m
    by_imei = {m["imei"]: m for m in existing if m.get("imei")}
    taken = {m["name"] for m in existing}

    def work(group: str, members: list[dict]) -> dict:
        members = sorted(members, key=lambda x: (x["iface"], x["path"]))
        entry: dict = {
            "key": group, "usb": group.split("-usb-")[-1] if "-usb-" in group else group,
            "ports": [m["path"] for m in members], "port": None, "status": "no_at",
            "manufacturer": None, "model": None, "imei": None, "operator": None, "signal": None, "sim": None,
            "modem": None, "modem_id": None,
        }
        # qo'shilgan modemning porti shu guruhda bo'lsa — tegmaymiz (worker ochib qo'ygan)
        for m in members:
            owner = used_real.get(m["real"])
            if owner:
                entry.update(status="added", port=m["path"], modem=owner["name"], modem_id=owner["id"])
                return entry
        for m in members:
            info = prober(m["path"])
            if info is not None:
                entry.update(info)
                entry["port"] = m["path"]
                entry["status"] = "new"
                other = by_imei.get(info.get("imei") or "")
                if other:       # shu modem boshqa USB portga ko'chirilgan
                    entry.update(status="moved", modem=other["name"], modem_id=other["id"])
                break
        return entry

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(work, g, ms) for g, ms in sorted(groups.items())]
        results = [f.result() for f in futures]

    # yangi modemlarga ketma-ket nom taklif qilamiz
    for r in results:
        if r["status"] == "new":
            r["suggest_name"] = _next_name(taken)
            taken.add(r["suggest_name"])
            op = (r.get("operator") or "").lower()
            r["suggest_balance_code"] = next((c for k, c in OPERATOR_BALANCE_CODES.items() if k in op), "*100#")
    return results


# ----------------------------------------------------------------------------- bazaga yozish / o'qish
def request_scan() -> str:
    """Web panel chaqiradi: worker ga skan so'rovi."""
    token = f"{utcnow().timestamp():.3f}"
    with session_scope() as s:
        st.set_(s, "scan_request", token)
        st.set_(s, "scan_status", "queued")
        st.set_(s, "scan_requested_at", utcnow().isoformat())
    return token


def read_state() -> dict:
    with session_scope() as s:
        status = st.get(s, "scan_status") or "idle"
        raw = st.get(s, "scan_result")
        return {
            "status": status,
            "requested_at": st.get(s, "scan_requested_at"),
            "finished_at": st.get(s, "scan_finished_at"),
            "error": st.get(s, "scan_error"),
            "results": json.loads(raw) if raw else [],
        }


def process_request() -> bool:
    """Worker: kutilayotgan so'rov bo'lsa skanerlaydi. True = skan bajarildi."""
    with session_scope() as s:
        req = st.get(s, "scan_request")
        if not req or req == st.get(s, "scan_handled"):
            return False
        st.set_(s, "scan_handled", req)
        st.set_(s, "scan_status", "running")
        existing = [{"id": m.id, "name": m.name, "port": m.port, "imei": m.imei} for m in s.query(Modem)]
    results: list[dict] = []
    error = ""
    try:
        results = scan(existing)
    except Exception as e:      # noqa: BLE001 — skan xatosi worker ni to'xtatmasin
        log.exception("skan xatosi")
        error = f"{e.__class__.__name__}: {e}"[:300]
    with session_scope() as s:
        st.set_(s, "scan_result", json.dumps(results, ensure_ascii=False))
        st.set_(s, "scan_error", error)
        st.set_(s, "scan_finished_at", utcnow().isoformat())
        st.set_(s, "scan_status", "error" if error else "done")
        n_new = sum(1 for r in results if r["status"] in ("new", "moved"))
        st.log(s, "info", "scan", f"Modem qidiruvi tugadi: {len(results)} ta USB qurilma, yangi: {n_new}")
    return True


class ScanWatcher(threading.Thread):
    """Worker ichida doimiy ishlaydi: har ~1.5 soniyada skan so'rovini tekshiradi."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="scan-watcher")
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.wait(1.5):
            try:
                process_request()
            except Exception:       # noqa: BLE001
                log.exception("scan watcher xatosi")
