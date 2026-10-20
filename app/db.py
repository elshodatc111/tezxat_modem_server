from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC (bazada UTC saqlanadi, panelda mahalliy vaqtga o'tkaziladi)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_kwargs: dict = {"pool_pre_ping": True, "future": True}
if config.DATABASE_URL.startswith("sqlite"):
    _kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
else:
    _kwargs.update(pool_recycle=1800, pool_size=10, max_overflow=10)

engine = create_engine(config.DATABASE_URL, **_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
