import json

from app import scanner
from app.bootstrap import ensure_defaults
from app.db import session_scope
from app.models import Modem


def ports(*items):
    return lambda: [{"path": p, "real": r, "group": g, "iface": i} for p, r, g, i in items]


def test_bypath_regex_groups_interfaces():
    m = scanner._BYPATH.match("pci-0000:00:1d.0-usb-0:1.2:1.3-port0")
    assert m and m.group("group") == "pci-0000:00:1d.0-usb-0:1.2" and m.group("iface") == "3"
    # xab orqali chuqurroq yo'l
    m = scanner._BYPATH.match("pci-0000:00:14.0-usb-0:2.4.1:1.0-port0")
    assert m.group("group").endswith("usb-0:2.4.1")


def test_scan_hub_with_several_modems_and_silent_ports():
    # 3 ta USB modem, har birida 4 port; faqat bittasi (3-interfeys) AT ga javob beradi
    items = []
    for dev in ("A", "B", "C"):
        for i in range(4):
            items.append((f"/dev/serial/by-path/{dev}-if{i}", f"/dev/ttyUSB{ord(dev) % 60}{i}", f"grp-{dev}", i))
    calls = []

    def prober(path):
        calls.append(path)
        if path.endswith("-if3"):
            return {"manufacturer": "ZTE", "model": "MF112", "imei": "86" + path[-8:-4].encode().hex()[:10].ljust(13, "0"),
                    "sim": "READY", "signal": 20, "operator": "Ucell"}
        return None

    res = scanner.scan([], lister=ports(*items), prober=prober)
    assert [r["status"] for r in res] == ["new", "new", "new"]
    assert [r["suggest_name"] for r in res] == ["Modem-1", "Modem-2", "Modem-3"]
    assert all(r["port"].endswith("-if3") for r in res)
    assert len(calls) == 12          # har bir portga bittadan (jim portlar ham)
    assert res[0]["suggest_balance_code"] == "*100#"


def test_scan_marks_added_moved_and_missing_at():
    items = [
        ("/dev/serial/by-path/X-if0", "/dev/ttyUSB0", "gx", 0),
        ("/dev/serial/by-path/X-if3", "/dev/ttyUSB3", "gx", 3),
        ("/dev/serial/by-path/Y-if0", "/dev/ttyUSB4", "gy", 0),
        ("/dev/serial/by-path/Z-if0", "/dev/ttyUSB8", "gz", 0),
    ]
    existing = [
        {"id": 1, "name": "Modem-1", "port": "/dev/ttyUSB3", "imei": "111"},   # X guruhi qo'shilgan
        {"id": 2, "name": "Modem-2", "port": "/dev/serial/by-path/old-port", "imei": "222"},
    ]

    def prober(path):
        if "Y-" in path:
            return {"manufacturer": "Huawei", "model": "E173", "imei": "222", "sim": "READY", "signal": 12, "operator": "Beeline"}
        return None

    res = {r["key"]: r for r in scanner.scan(existing, lister=ports(*items), prober=prober)}
    assert res["gx"]["status"] == "added" and res["gx"]["modem"] == "Modem-1"
    assert res["gy"]["status"] == "moved" and res["gy"]["modem_id"] == 2
    assert res["gz"]["status"] == "no_at" and res["gz"]["port"] is None


def test_beeline_balance_code_and_name_gap():
    items = [("/dev/serial/by-path/Q-if0", "/dev/ttyUSB0", "gq", 0)]
    existing = [{"id": 1, "name": "Modem-1", "port": "mock://1", "imei": None}]
    res = scanner.scan(existing, lister=ports(*items),
                       prober=lambda p: {"manufacturer": None, "model": None, "imei": "9", "sim": "READY", "signal": 5, "operator": "Beeline UZ"})
    assert res[0]["suggest_name"] == "Modem-2" and res[0]["suggest_balance_code"] == "*102#"


def test_process_request_roundtrip(monkeypatch):
    ensure_defaults()
    with session_scope() as s:
        s.add(Modem(name="Modem-1", port="mock://1"))
    monkeypatch.setattr(scanner, "list_serial_ports", lambda: [])
    assert scanner.process_request() is False          # so'rov yo'q
    scanner.request_scan()
    assert scanner.read_state()["status"] == "queued"
    assert scanner.process_request() is True
    st = scanner.read_state()
    assert st["status"] == "done" and st["results"] == [] and st["finished_at"]
    assert scanner.process_request() is False          # bir xil so'rov ikki marta bajarilmaydi
