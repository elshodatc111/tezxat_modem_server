"""Web panel orqali o'zgartiriladigan sozlamalar (bazada)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import config
from .models import Event, Setting

DEFAULTS: dict[str, str] = {
    "hosting_url": "",              # masalan https://sms.example.uz  (hosting ko'chirilsa shu yerda o'zgartiriladi)
    "gateway_token": "",
    "poll_interval": "3",           # navbatni tekshirish oralig'i (soniya)
    "long_poll_wait": "0",          # >0 bo'lsa hosting shuncha soniya kutib javob beradi (hosting PHP vaqt limitiga e'tibor)
    "pull_batch": "5",
    "local_queue_cap": "30",        # mahalliy navbat shundan oshsa, hostingdan yangi olinmaydi
    "heartbeat_interval": "15",
    "max_send_attempts": "3",
    "request_delivery_report": "1",
    "http_timeout": "30",
    "min_balance": "0",             # SIM balansi shundan (so'm) past bo'lsa modem SMS yuborishdan to'xtaydi (0 = nazorat o'chiq)
    "balance_check_every": "5",      # har N ta SMS dan keyin balans qayta tekshiriladi
    "balance_check_minutes": "10",   # balansni avtomatik tekshirish oralig'i (modemda avto-USSD yo'q bo'lsa)
    "keep_days": "14",              # yakunlangan va hisobot yuborilgan SMS lar saqlanish muddati
    "worker_name": config.WORKER_NAME,
    "scan_request": "",             # modem qidirish so'rovi (web -> worker)
    "scan_handled": "",
    "scan_status": "",
    "scan_result": "",
    "scan_error": "",
    "scan_requested_at": "",
    "scan_finished_at": "",
    "hosting_last_ok": "",
    "hosting_last_error": "",
}


def get(session: Session, key: str) -> str:
    row = session.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    return DEFAULTS.get(key, "")


def get_int(session: Session, key: str, default: int = 0) -> int:
    try:
        return int(float(get(session, key)))
    except (TypeError, ValueError):
        return default


def get_all(session: Session) -> dict[str, str]:
    out = dict(DEFAULTS)
    for row in session.query(Setting).all():
        if row.value is not None:
            out[row.key] = row.value
    return out


def set_(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def log(session: Session, level: str, source: str, message: str) -> None:
    session.add(Event(level=level, source=source[:40], message=message[:500]))
