"""Muhit sozlamalari (.env). Hosting manzili va token esa web panelda (bazada) saqlanadi."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # python-dotenv o'rnatilmagan bo'lsa ham ishlayveradi
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# MySQL: mysql+pymysql://user:pass@127.0.0.1:3306/sms_modem?charset=utf8mb4
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'modem.db'}")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin12345")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8088"))
WORKER_NAME = os.getenv("WORKER_NAME", "ubuntu-server-1")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")
# MariaDB 10.6+ / MySQL 8+ da SKIP LOCKED qo'llab-quvvatlanadi. Eski versiyada 0 qiling.
DB_SKIP_LOCKED = _bool(os.getenv("DB_SKIP_LOCKED"), True)
# Demo/test: "mock://1" portli modemlarni avtomatik yaratish
CREATE_DEMO_MODEM = _bool(os.getenv("CREATE_DEMO_MODEM"), False)
SESSION_HTTPS_ONLY = _bool(os.getenv("SESSION_HTTPS_ONLY"), False)
