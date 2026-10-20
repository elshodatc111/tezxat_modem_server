"""Birinchi ishga tushirishda kerakli boshlang'ich ma'lumotlarni yaratish.

worker va web bir vaqtda ishga tushishi mumkin, shuning uchun har bir qadam
poyga holatiga (IntegrityError / jadval allaqachon yaratilgan) chidamli.
"""
from __future__ import annotations

import time

from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from . import config
from .db import init_db, session_scope
from .models import Admin, Modem
from .security import hash_password


def _init_db_safe() -> None:
    for attempt in range(5):
        try:
            init_db()
            return
        except (OperationalError, ProgrammingError):
            # boshqa jarayon jadvalni hozirgina yaratgan bo'lishi mumkin
            if attempt == 4:
                raise
            time.sleep(1 + attempt)


def _add_if_empty(model, make) -> None:
    try:
        with session_scope() as s:
            if s.query(model).count() == 0:
                for obj in make():
                    s.add(obj)
    except IntegrityError:
        pass  # boshqa jarayon allaqachon yaratgan


def ensure_defaults() -> None:
    _init_db_safe()
    _add_if_empty(Admin, lambda: [Admin(username=config.ADMIN_USER, password_hash=hash_password(config.ADMIN_PASSWORD))])
    if config.CREATE_DEMO_MODEM:
        _add_if_empty(Modem, lambda: [
            Modem(name="Demo-1", port="mock://1", ussd_balance_code="*100#", ussd_package_code="*107#", min_interval=0.5),
            Modem(name="Demo-2", port="mock://2", ussd_balance_code="*100#", ussd_package_code="*107#", min_interval=0.5),
        ])
