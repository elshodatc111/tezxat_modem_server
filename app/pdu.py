"""SMS PDU kodlash/dekodlash (GSM 03.40): GSM-7, UCS-2, uzun (concatenated) SMS, status report.

Qoidalar PHP tomondagi SmsCounter bilan bir xil: GSM-7 — 160/153, UCS-2 — 70/67.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)
GSM7_EXT = {0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\", 0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "€"}

_BASIC_ENC = {c: i for i, c in enumerate(GSM7_BASIC) if i != 0x1B}
_EXT_ENC = {c: i for i, c in GSM7_EXT.items()}
ESC = 0x1B


def gsm7_septets(text: str) -> list[list[int]] | None:
    """Matn GSM-7 da ifodalanadimi? Ha bo'lsa, har belgi uchun septetlar ro'yxati; yo'q bo'lsa None."""
    out: list[list[int]] = []
    for ch in text:
        if ch in _BASIC_ENC:
            out.append([_BASIC_ENC[ch]])
        elif ch in _EXT_ENC:
            out.append([ESC, _EXT_ENC[ch]])
        else:
            return None
    return out


def analyze(text: str) -> dict:
    """{'encoding','length','parts'} — PHP SmsCounter::analyze bilan bir xil natija."""
    sep = gsm7_septets(text)
    if sep is not None:
        n = sum(len(x) for x in sep)
        parts = 1 if n <= 160 else math.ceil(n / 153)
        return {"encoding": "gsm7", "length": n, "parts": max(1, parts)}
    units = len(text.encode("utf-16-be")) // 2
    parts = 1 if units <= 70 else math.ceil(units / 67)
    return {"encoding": "ucs2", "length": units, "parts": max(1, parts)}


def pack_septets(septets: list[int], bit_offset: int = 0) -> bytes:
    """Septetlarni 7-bit ketma-ketlikda baytlarga joylaydi (bit_offset — UDH dan keyingi to'ldiruvchi bitlar)."""
    value = 0
    for i, s in enumerate(septets):
        value |= (s & 0x7F) << (bit_offset + 7 * i)
    nbytes = math.ceil((bit_offset + 7 * len(septets)) / 8)
    return value.to_bytes(nbytes, "little")


def unpack_septets(data: bytes, count: int, bit_offset: int = 0) -> list[int]:
    value = int.from_bytes(data, "little") >> bit_offset
    return [(value >> (7 * i)) & 0x7F for i in range(count)]


def gsm7_decode(septets: list[int]) -> str:
    out, esc = [], False
    for s in septets:
        if esc:
            out.append(GSM7_EXT.get(s, " "))
            esc = False
        elif s == ESC:
            esc = True
        else:
            out.append(GSM7_BASIC[s] if s < 128 else "?")
    return "".join(out)


def _swap_digits(digits: str) -> str:
    if len(digits) % 2:
        digits += "F"
    return "".join(digits[i + 1] + digits[i] for i in range(0, len(digits), 2))


def _split_gsm7(chars: list[list[int]], limit: int) -> list[list[int]]:
    """Septetlarni qismlarga bo'ladi; ikki septetli belgi (ESC+x) bo'linib qolmaydi."""
    parts: list[list[int]] = []
    cur: list[int] = []
    for c in chars:
        if len(cur) + len(c) > limit:
            parts.append(cur)
            cur = []
        cur.extend(c)
    if cur or not parts:
        parts.append(cur)
    return parts


def _split_ucs2(text: str, limit_units: int) -> list[bytes]:
    data = text.encode("utf-16-be")
    units = [data[i:i + 2] for i in range(0, len(data), 2)]
    parts: list[bytes] = []
    cur: list[bytes] = []
    i = 0
    while i < len(units):
        # surrogate juftligi bo'linmasligi uchun
        take = 2 if 0xD8 <= units[i][0] <= 0xDB and i + 1 < len(units) else 1
        if len(cur) + take > limit_units:
            parts.append(b"".join(cur))
            cur = []
        cur.extend(units[i:i + take])
        i += take
    if cur or not parts:
        parts.append(b"".join(cur))
    return parts


@dataclass
class PduPart:
    pdu: str       # to'liq PDU (SMSC "00" bilan), hex
    tpdu_len: int  # AT+CMGS=<len> uchun uzunlik (SMSC siz)
    index: int
    total: int


def encode_submit(phone: str, text: str, request_report: bool = True, ref: int | None = None) -> list[PduPart]:
    """Matnni bir yoki bir nechta SMS-SUBMIT PDU larga aylantiradi."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not 5 <= len(digits) <= 15:
        raise ValueError(f"Noto'g'ri telefon raqami: {phone!r}")

    sep = gsm7_septets(text)
    is_gsm = sep is not None
    if is_gsm:
        total_sep = sum(len(x) for x in sep)
        pieces = [x for x in [_split_gsm7(sep, 160)[0]]] if total_sep <= 160 else _split_gsm7(sep, 153)
    else:
        n_units = len(text.encode("utf-16-be")) // 2
        pieces = [_split_ucs2(text, 70)[0]] if n_units <= 70 else _split_ucs2(text, 67)
    total = len(pieces)
    multipart = total > 1
    ref = (random.randint(1, 255) if ref is None else ref) & 0xFF

    first = 0x01
    if request_report:
        first |= 0x20
    if multipart:
        first |= 0x40
    addr = f"{len(digits):02X}91{_swap_digits(digits)}"
    dcs = "00" if is_gsm else "08"

    out: list[PduPart] = []
    for i, piece in enumerate(pieces, start=1):
        udh = bytes([0x05, 0x00, 0x03, ref, total, i]) if multipart else b""
        if is_gsm:
            if multipart:
                body = pack_septets(piece, bit_offset=1)       # UDH 48 bit + 1 bit fill = 49 = 7 septet
                udl = 7 + len(piece)
            else:
                body = pack_septets(piece)
                udl = len(piece)
        else:
            body = piece
            udl = len(udh) + len(body)
        ud = udh + body
        tpdu = f"{first:02X}00{addr}00{dcs}{udl:02X}{ud.hex().upper()}"
        out.append(PduPart(pdu="00" + tpdu, tpdu_len=len(tpdu) // 2, index=i, total=total))
    return out


@dataclass
class StatusReport:
    mr: int
    recipient: str
    status: int

    @property
    def delivered(self) -> bool:
        return self.status < 0x20          # 0x00..0x1F — yetkazildi

    @property
    def final(self) -> bool:
        return self.status < 0x20 or self.status >= 0x40   # 0x20..0x3F — hali urinilmoqda (vaqtincha)


def _unswap(hexdigits: str) -> str:
    out = ""
    for i in range(0, len(hexdigits), 2):
        out += hexdigits[i + 1] + hexdigits[i]
    return out.rstrip("Ff")


def decode_status_report(pdu_hex: str) -> StatusReport:
    """SMS-STATUS-REPORT PDU ni o'qiydi."""
    p = pdu_hex.strip().upper()
    smsc_len = int(p[0:2], 16)
    i = 2 + smsc_len * 2
    first = int(p[i:i + 2], 16)
    if first & 0x03 != 0x02:
        raise ValueError("Bu status report emas")
    i += 2
    mr = int(p[i:i + 2], 16)
    i += 2
    ra_len = int(p[i:i + 2], 16)
    i += 4  # uzunlik + tur
    ra_bytes = math.ceil(ra_len / 2)
    recipient = _unswap(p[i:i + ra_bytes * 2])
    i += ra_bytes * 2
    i += 14  # SCTS
    i += 14  # DT
    st = int(p[i:i + 2], 16)
    return StatusReport(mr=mr, recipient=recipient, status=st)


def decode_deliver_text(pdu_hex: str) -> str:
    """(Test/diagnostika uchun) SMS-SUBMIT PDU dan matnni qayta tiklaydi."""
    p = pdu_hex.strip().upper()
    i = 2 + int(p[0:2], 16) * 2
    first = int(p[i:i + 2], 16)
    i += 2 + 2  # first + MR
    da_len = int(p[i:i + 2], 16)
    i += 4 + math.ceil(da_len / 2) * 2
    i += 2  # PID
    dcs = int(p[i:i + 2], 16)
    i += 2
    udl = int(p[i:i + 2], 16)
    i += 2
    ud = bytes.fromhex(p[i:])
    has_udh = bool(first & 0x40)
    udh_len = (ud[0] + 1) if has_udh else 0
    if dcs == 0x08:
        return ud[udh_len:udh_len + (udl - udh_len) ].decode("utf-16-be")
    offset = 0
    skip_septets = 0
    if has_udh:
        skip_septets = math.ceil(udh_len * 8 / 7)
        offset = skip_septets * 7 - udh_len * 8
    sept = unpack_septets(ud[udh_len:], udl - skip_septets, offset)
    return gsm7_decode(sept)
