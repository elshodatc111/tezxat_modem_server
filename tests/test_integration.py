import time

import pytest

from app import worker
from app import settings_store as st
from app.bootstrap import ensure_defaults
from app.db import session_scope
from app.models import Modem, ModemCommand, Outbox
from tests.fake_hosting import FakeHosting


def wait_for(cond, timeout=20, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def system():
    ensure_defaults()
    with session_scope() as s:
        s.add(Modem(name="M1", port="mock://t1", min_interval=0.2, ussd_balance_code="*100#", ussd_package_code="*107#"))
        s.add(Modem(name="M2", port="mock://t2", min_interval=0.2, ussd_balance_code="*100#", ussd_package_code="*107#"))
        st.set_(s, "hosting_url", "http://fake")
        st.set_(s, "gateway_token", FakeHosting.TOKEN)
        st.set_(s, "poll_interval", "0.5")
        st.set_(s, "heartbeat_interval", "5")
    host = FakeHosting()
    manager = worker.ModemManager()
    sync = worker.SyncRunner(client_factory=host.client_factory())
    manager.tick()
    # ikkala modem ulanib, "online" bo'lguncha kutamiz
    def online():
        with session_scope() as s:
            return s.query(Modem).filter(Modem.status.in_(("online", "busy"))).count() == 2
    assert wait_for(online, timeout=20)
    sync.start()
    yield host, manager, sync
    sync.stop()
    manager.stop_all()
    sync.join(timeout=5)


def test_messages_flow_through_two_modems(system):
    host, manager, sync = system
    ids = [host.add(f"+99890123456{i}", f"Kod: {1000 + i}") for i in range(6)]
    long_id = host.add("+998931112233", "Привет! " * 25)       # UCS-2, bir necha qism
    assert wait_for(lambda: all(host.status(i) == "delivered" for i in ids + [long_id]), timeout=30), {i: host.status(i) for i in ids + [long_id]}

    m1, m2 = worker._MOCKS["mock://t1"], worker._MOCKS["mock://t2"]
    assert len(m1.sent) + len(m2.sent) == 7
    assert m1.sent and m2.sent, "ikkala modem ham ishlatilishi kerak (avtomatik taqsimot)"
    sent_texts = [t for _, t in m1.sent + m2.sent]
    assert ("Привет! " * 25) in sent_texts
    assert wait_for(lambda: len(host.heartbeats) >= 1)
    assert host.heartbeats[0]["modems"][0]["name"] == "M1"

    with session_scope() as s:
        assert s.query(Outbox).count() == 7


def test_failure_after_retries_is_reported(system):
    host, manager, sync = system
    for t in worker._MOCKS.values():
        t.fail_next = 99
    mid = host.add("+998901234567", "xato bo'lishi kerak")
    assert wait_for(lambda: host.status(mid) == "failed", timeout=30)
    assert "500" in (host.messages[mid]["error"] or "")
    with session_scope() as s:
        o = s.query(Outbox).one()
        assert o.attempts == 3 and o.status == "failed"


def test_retry_succeeds_on_other_attempt(system):
    host, manager, sync = system
    worker._MOCKS["mock://t1"].fail_next = 1
    worker._MOCKS["mock://t2"].fail_next = 0
    mid = host.add("+998901234567", "ikkinchi urinishda ketadi")
    assert wait_for(lambda: host.status(mid) in ("sent", "delivered"), timeout=30)


def test_hosting_down_then_recovers(system):
    host, manager, sync = system
    host.fail_requests = 3
    mid = host.add("+998901234567", "hosting uzilib tiklanadi")
    assert wait_for(lambda: host.status(mid) in ("sent", "delivered"), timeout=40)
    with session_scope() as s:
        assert st.get(s, "hosting_last_error") == ""


def test_ussd_command_and_package_text(system):
    host, manager, sync = system
    assert wait_for(lambda: worker._MOCKS.get("mock://t1") and True)
    with session_scope() as s:
        m1 = s.query(Modem).filter_by(name="M1").one()
        c1 = ModemCommand(modem_id=m1.id, kind="ussd_balance")
        c2 = ModemCommand(modem_id=m1.id, kind="ussd_package")
        c3 = ModemCommand(modem_id=m1.id, kind="send_test", payload='{"to": "+998901112233", "text": "test"}')
        s.add_all([c1, c2, c3])
        s.flush()
        ids = (c1.id, c2.id, c3.id)

    def done():
        with session_scope() as s:
            return all(s.get(ModemCommand, i).status in ("done", "error") for i in ids)

    assert wait_for(done, timeout=30)
    with session_scope() as s:
        m1 = s.query(Modem).filter_by(name="M1").one()
        assert "12 500" in m1.balance_text
        assert "480" in m1.package_text
        assert all(s.get(ModemCommand, i).status == "done" for i in ids)
    assert ("998901112233", "test") in worker._MOCKS["mock://t1"].sent


def test_no_pull_when_no_modem_online():
    ensure_defaults()
    with session_scope() as s:
        s.add(Modem(name="X", port="mock://dead", enabled=False))
        st.set_(s, "hosting_url", "http://fake")
        st.set_(s, "gateway_token", FakeHosting.TOKEN)
    host = FakeHosting()
    host.add("+998901234567", "hostingda qoladi")
    sync = worker.SyncRunner(client_factory=host.client_factory())
    client = host.client_factory()("http://fake", FakeHosting.TOKEN)
    with session_scope() as s:
        cfg = st.get_all(s)
    assert sync.pull(client, cfg) == 0
    assert host.status(1) == "queued"
