import time

import pytest

from app import settings_store as st
from app import worker
from app.balance import parse_balance
from app.bootstrap import ensure_defaults
from app.db import session_scope
from app.models import Modem, ModemCommand, Outbox
from tests.fake_hosting import FakeHosting
from tests.test_integration import wait_for


@pytest.mark.parametrize("text,expected", [
    ("Balans: 12 500 som. Bonus: 0", 12500),
    ("Sizning balansingiz 1250.50 so'm", 1250.5),
    ("Balans: 0 so'm", 0),
    ("Hisobingizda 150 sum qoldi", 150),
    ("Balance: 1,250.75 UZS", 1250.75),
    ("Баланс: 12.500 сум", 12500),
    ("Balans: -300 so'm", -300),
    ("Xato: USSD javob kelmadi", None),
    ("", None),
    (None, None),
])
def test_parse_balance(text, expected):
    assert parse_balance(text) == expected


def _setup(min_balance):
    ensure_defaults()
    with session_scope() as s:
        s.add(Modem(name="M1", port="mock://t1", min_interval=0.2, ussd_balance_code="*100#"))
        st.set_(s, "hosting_url", "http://fake")
        st.set_(s, "gateway_token", FakeHosting.TOKEN)
        st.set_(s, "poll_interval", "0.5")
        st.set_(s, "min_balance", str(min_balance))


def _status():
    with session_scope() as s:
        return s.query(Modem).filter_by(name="M1").one().status


def test_modem_blocked_when_sim_has_no_funds_and_recovers():
    _setup(min_balance=20000)           # mock balans = 12 500 < 20 000
    host = FakeHosting()
    manager = worker.ModemManager()
    sync = worker.SyncRunner(client_factory=host.client_factory())
    manager.tick()
    try:
        assert wait_for(lambda: _status() == "no_funds", timeout=30), _status()
        sync.start()
        mid = host.add("+998901234567", "pul yo'q: yuborilmasligi kerak")
        time.sleep(3)                                # bir necha sinxronlash aylanishi
        assert host.pulls == 0                       # yaroqli modem yo'q: hostingdan SMS olinmaydi
        assert host.status(mid) == "queued"
        with session_scope() as s:
            assert s.query(Outbox).count() == 0
        assert not worker._MOCKS["mock://t1"].sent

        # SIM to'ldirildi: balansni qayta tekshirish buyrug'i
        worker._MOCKS["mock://t1"].balance_text = "Balans: 50 000 som"
        with session_scope() as s:
            m = s.query(Modem).filter_by(name="M1").one()
            s.add(ModemCommand(modem_id=m.id, kind="ussd_balance"))
        assert wait_for(lambda: _status() == "online", timeout=20), _status()
        assert wait_for(lambda: host.status(mid) in ("sent", "delivered"), timeout=30)
    finally:
        sync.stop()
        manager.stop_all()
        sync.join(timeout=5)


def test_pending_sms_failed_when_funds_run_out():
    _setup(min_balance=1000)
    manager = worker.ModemManager()
    manager.tick()
    try:
        assert wait_for(lambda: _status() == "online", timeout=20)
        with session_scope() as s:
            m = s.query(Modem).filter_by(name="M1").one()
            m.daily_limit = 1            # 2-SMS lokal navbatda qolib ketadi
            m.sent_today = 1
            m.today = worker.local_today()
            s.add(Outbox(remote_id=1, phone="+998901234567", text="a"))
        worker._MOCKS["mock://t1"].balance_text = "Balans: 0 so'm"
        with session_scope() as s:
            m = s.query(Modem).filter_by(name="M1").one()
            s.add(ModemCommand(modem_id=m.id, kind="ussd_balance"))
        assert wait_for(lambda: _status() == "no_funds", timeout=20)
        with session_scope() as s:
            o = s.query(Outbox).one()
            assert o.status == "failed" and "mablag" in o.error
    finally:
        manager.stop_all()
