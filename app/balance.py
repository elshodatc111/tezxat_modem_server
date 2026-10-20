"""USSD javobidan SIM balansini (so'mda) ajratib olish."""
from __future__ import annotations

import re

_NUM = r"-?\d[\d\s.,']*"
_CUR = r"(?:so['’`ʻʼ]?m|sum|som|сум|сўм|uzs)\b"
_WORDS = r"(?:balans\w*|balance|баланс\w*|hisob\w*|qoldiq\w*|остаток|счет|счёт)"


def _to_float(raw: str) -> float | None:
    s = raw.strip().replace("'", "").replace(" ", "").replace(" ", "").rstrip(".,")
    if not s or s in ("-", "+"):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        head, _, tail = s.rpartition(sep)
        if len(tail) == 3 and head and len(head) <= 3 or s.count(sep) > 1:
            s = s.replace(sep, "")          # minglik ajratgich: 12.500 -> 12500
        else:
            s = head + "." + tail            # o'nlik: 125.50
    try:
        return float(s)
    except ValueError:
        return None


def parse_balance(text: str | None) -> float | None:
    """Balans qiymatini qaytaradi; aniqlab bo'lmasa None (shunda SIM bloklanmaydi)."""
    if not text:
        return None
    t = text.strip()
    m = re.search(rf"({_NUM})\s*{_CUR}", t, re.I)
    if m:
        return _to_float(m.group(1))
    m = re.search(rf"{_WORDS}\D{{0,25}}?({_NUM})", t, re.I)
    if m:
        return _to_float(m.group(1))
    return None
