from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class Modem(Base):
    __tablename__ = "modems"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    port: Mapped[str] = mapped_column(String(200))                    # /dev/serial/by-id/... yoki /dev/ttyUSB0 yoki mock://1
    baudrate: Mapped[int] = mapped_column(Integer, default=115200)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pin: Mapped[str | None] = mapped_column(String(10), nullable=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=0)      # 0 = cheksiz
    min_interval: Mapped[float] = mapped_column(Float, default=3.0)   # SMS lar orasidagi pauza (soniya)
    ussd_balance_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ussd_package_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    auto_ussd_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = o'chiq
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # holat (worker yangilaydi)
    status: Mapped[str] = mapped_column(String(20), default="offline")   # offline|online|busy|error|disabled
    signal: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 0..31 (CSQ)
    operator: Mapped[str | None] = mapped_column(String(60), nullable=True)
    sim_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    imei: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    balance_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    package_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    package_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_today: Mapped[int] = mapped_column(Integer, default=0)
    today: Mapped[date | None] = mapped_column(Date, nullable=True)
    sent_total: Mapped[int] = mapped_column(Integer, default=0)
    failed_total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Outbox(Base):
    """Hostingdan olingan SMS lar. Mahalliy navbat."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    remote_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    phone: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    encoding: Mapped[str] = mapped_column(String(8), default="gsm7")
    parts: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default="pending")   # pending|sending|sent|delivered|failed
    modem_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reported_status: Mapped[str] = mapped_column(String(12), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_outbox_status_id", "status", "id"),)


class SmsRef(Base):
    """Yuborilgan SMS qismlarining MR raqami — status report (yetkazildi) ni moslash uchun."""
    __tablename__ = "sms_refs"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    outbox_id: Mapped[int] = mapped_column(BigInteger, index=True)
    modem_id: Mapped[int] = mapped_column(Integer)
    mr: Mapped[int] = mapped_column(Integer)
    part: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[int] = mapped_column(Integer, default=1)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_refs_modem_mr", "modem_id", "mr"),)


class ModemCommand(Base):
    """Web panel -> worker buyruqlari (USSD, test SMS, qayta ishga tushirish)."""
    __tablename__ = "modem_commands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    modem_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(20))      # ussd | ussd_balance | ussd_package | send_test | reinit | info
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="pending")   # pending|running|done|error
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(10), default="info")
    source: Mapped[str] = mapped_column(String(40), default="")
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
