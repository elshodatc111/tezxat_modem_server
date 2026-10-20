import re

from fastapi.testclient import TestClient

from app.bootstrap import ensure_defaults
from app.db import session_scope
from app.models import Modem, ModemCommand, Outbox


def login(client):
    r = client.get("/login")
    assert r.status_code == 200
    r = client.post("/login", data={"username": "admin", "password": "secret123"}, follow_redirects=False)
    assert r.status_code == 303


def csrf(client, path="/settings"):
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def make_client():
    from app.web import app

    ensure_defaults()
    return TestClient(app)


def test_requires_login():
    c = make_client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/state").status_code == 401


def test_wrong_password_and_csrf():
    c = make_client()
    r = c.post("/login", data={"username": "admin", "password": "bad"}, follow_redirects=False)
    assert r.status_code == 303
    login(c)
    # CSRF siz POST rad etiladi
    r = c.post("/modems", data={"name": "X", "port": "mock://x"})
    assert r.status_code == 403


def test_full_panel_flow():
    c = make_client()
    login(c)
    for p in ("/", "/modems", "/outbox", "/settings", "/events"):
        assert c.get(p).status_code == 200, p
    tok = csrf(c, "/modems")
    r = c.post("/modems", data={"csrf": tok, "name": "Modem-1", "port": "mock://1", "ussd_balance_code": "*100#"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        m = s.query(Modem).one()
        mid = m.id
    assert c.get(f"/modems/{mid}").status_code == 200
    # buyruq yaratish
    r = c.post(f"/modems/{mid}/command", data={"csrf": tok, "kind": "info"})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert c.get(f"/api/commands/{cid}").json()["status"] == "pending"
    assert c.post(f"/modems/{mid}/command", data={"csrf": tok, "kind": "hack"}).status_code == 400
    # sozlamalar
    r = c.post("/settings", data={"csrf": tok, "hosting_url": "https://sms.example.uz/", "gateway_token": "gw_secret1234", "poll_interval": "2", "request_delivery_report": "1"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/settings").text
    assert "https://sms.example.uz" in page and "1234" in page and "gw_secret1234" not in page   # token ochiq ko'rsatilmaydi
    r = c.post("/settings", data={"csrf": tok, "hosting_url": "ftp://x"}, follow_redirects=False)
    assert "http" in c.get("/settings").text
    # navbat amallari
    with session_scope() as s:
        s.add(Outbox(remote_id=5, phone="+998901234567", text="salom", status="failed", attempts=3, error="x"))
        s.add(Outbox(remote_id=6, phone="+998901234568", text="salom2", status="pending"))
    assert "salom" in c.get("/outbox").text
    with session_scope() as s:
        failed_id = s.query(Outbox).filter_by(remote_id=5).one().id
        pend_id = s.query(Outbox).filter_by(remote_id=6).one().id
    c.post(f"/outbox/{failed_id}/retry", data={"csrf": tok})
    c.post(f"/outbox/{pend_id}/fail", data={"csrf": tok})
    with session_scope() as s:
        assert s.get(Outbox, failed_id).status == "pending" and s.get(Outbox, failed_id).reported_status == ""
        assert s.get(Outbox, pend_id).status == "failed"
    # o'chirish
    c.post(f"/modems/{mid}/delete", data={"csrf": tok})
    with session_scope() as s:
        assert s.query(Modem).count() == 0


def test_settings_test_connection_without_config():
    c = make_client()
    login(c)
    tok = csrf(c)
    r = c.post("/settings/test", data={"csrf": tok})
    assert r.json()["ok"] is False


def test_password_change():
    c = make_client()
    login(c)
    tok = csrf(c)
    c.post("/settings/password", data={"csrf": tok, "current": "secret123", "new": "newpass1234"})
    c2 = make_client()
    r = c2.post("/login", data={"username": "admin", "password": "newpass1234"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_scan_api_and_quick_add():
    from app import scanner

    c = make_client()
    login(c)
    tok = csrf(c, "/modems")
    assert "Modemlarni qidirish" in c.get("/modems").text
    r = c.post("/api/scan", data={"csrf": tok})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    r = c.post("/api/scan", data={"csrf": tok})                  # takroriy bosish yangi so'rov yaratmaydi
    assert r.json()["status"] == "queued"
    st = c.get("/api/scan").json()
    assert st["status"] == "queued" and st["results"] == []

    # qidiruv natijasidan qo'shish
    r = c.post("/modems/quick-add", data={"csrf": tok, "name": "Modem-1", "port": "/dev/serial/by-path/pci-usb-0:1.2:1.3-port0", "ussd_balance_code": "*102#"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        m = s.query(Modem).one()
        assert m.name == "Modem-1" and m.ussd_balance_code == "*102#"
        mid = m.id
    # nom va port takrorlanmaydi
    for data in ({"name": "Modem-1", "port": "/dev/ttyUSB9"}, {"name": "Boshqa", "port": "/dev/serial/by-path/pci-usb-0:1.2:1.3-port0"}):
        c.post("/modems/quick-add", data={"csrf": tok, **data}, follow_redirects=False)
    with session_scope() as s:
        assert s.query(Modem).count() == 1
    # noto'g'ri port rad etiladi
    c.post("/modems/quick-add", data={"csrf": tok, "name": "X", "port": "/etc/passwd"}, follow_redirects=False)
    with session_scope() as s:
        assert s.query(Modem).count() == 1
    # portni yangilash
    r = c.post(f"/modems/{mid}/set-port", data={"csrf": tok, "port": "/dev/serial/by-path/new-port"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        assert s.get(Modem, mid).port.endswith("new-port")
