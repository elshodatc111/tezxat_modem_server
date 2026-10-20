import pytest

from app import pdu


def test_known_vector_hellohello():
    parts = pdu.encode_submit("+998901234567", "hellohello", request_report=False)
    assert len(parts) == 1
    assert parts[0].pdu.endswith("0AE8329BFD4697D9EC37")  # UDL=10 + packed septets (klassik namuna)


def test_header_and_length():
    p = pdu.encode_submit("+998901234567", "hellohello", request_report=True)[0]
    assert p.pdu.startswith("0021000C91")      # SMSC=00, SUBMIT+SRR, MR=00, DA len=12, intl
    assert p.tpdu_len == len(p.pdu) // 2 - 1


def test_phone_swap():
    p = pdu.encode_submit("+998901234567", "a", request_report=False)[0]
    assert "0C91" + "999810325476" in p.pdu     # 998901234567 -> juftliklar almashtirilgan


def test_roundtrip_gsm7_single():
    txt = "Kod: 4821 {ok} €"
    p = pdu.encode_submit("998901234567", txt)[0]
    assert pdu.decode_deliver_text(p.pdu) == txt


def test_roundtrip_ucs2_uzbek_and_cyrillic():
    for txt in ["Oʻzbekiston ✓", "Привет, мир!", "😀 emoji"]:
        parts = pdu.encode_submit("998901234567", txt)
        assert len(parts) == 1
        assert pdu.decode_deliver_text(parts[0].pdu) == txt
        assert parts[0].pdu.split("0008")[0] is not None


def test_dcs_values():
    assert "0008" in pdu.encode_submit("998901234567", "Привет")[0].pdu
    assert "0000" in pdu.encode_submit("998901234567", "hello")[0].pdu


@pytest.mark.parametrize("n,expected", [(160, 1), (161, 2), (306, 2), (307, 3)])
def test_gsm7_parts(n, expected):
    parts = pdu.encode_submit("998901234567", "a" * n)
    assert len(parts) == expected
    assert pdu.analyze("a" * n)["parts"] == expected


@pytest.mark.parametrize("n,expected", [(70, 1), (71, 2), (134, 2), (135, 3)])
def test_ucs2_parts(n, expected):
    parts = pdu.encode_submit("998901234567", "я" * n)
    assert len(parts) == expected
    assert pdu.analyze("я" * n)["parts"] == expected


def test_multipart_reassembles():
    txt = "".join(chr(ord("a") + i % 26) for i in range(400))
    parts = pdu.encode_submit("998901234567", txt, ref=7)
    assert [p.index for p in parts] == [1, 2, 3]
    assert all(p.total == 3 for p in parts)
    assert "".join(pdu.decode_deliver_text(p.pdu) for p in parts) == txt
    ucs = "Привет " * 60
    parts = pdu.encode_submit("998901234567", ucs, ref=9)
    assert "".join(pdu.decode_deliver_text(p.pdu) for p in parts) == ucs


def test_escape_char_not_split():
    # 152 oddiy belgi + '{' (2 septet) = 154 > 153 => '{' keyingi qismga o'tishi kerak
    txt = "a" * 152 + "{" + "b" * 200
    parts = pdu.encode_submit("998901234567", txt)
    assert "".join(pdu.decode_deliver_text(p.pdu) for p in parts) == txt


def test_analyze_matches_php_rules():
    assert pdu.analyze("Salom")["encoding"] == "gsm7"
    assert pdu.analyze("Kod: 1234 o'tdi")["encoding"] == "gsm7"
    assert pdu.analyze("Oʻzbekiston")["encoding"] == "ucs2"
    assert pdu.analyze("😀")["length"] == 2


def test_status_report_decode():
    # SMSC yo'q, STATUS-REPORT, MR=0x2A, RA=998901234567, ST=0x00
    pdu_hex = "00" + "06" + "2A" + "0C91" + "999810325476" + "62016012053200" + "62016012053300" + "00"
    r = pdu.decode_status_report(pdu_hex)
    assert r.mr == 0x2A and r.recipient == "998901234567" and r.delivered and r.final
    pdu_failed = pdu_hex[:-2] + "41"
    r2 = pdu.decode_status_report(pdu_failed)
    assert not r2.delivered and r2.final
    pdu_temp = pdu_hex[:-2] + "22"
    r3 = pdu.decode_status_report(pdu_temp)
    assert not r3.delivered and not r3.final
