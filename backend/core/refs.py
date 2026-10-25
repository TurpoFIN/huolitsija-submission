"""Human-readable sequential references and Finnish payment references."""
from datetime import datetime, timezone

from pymongo import ReturnDocument

from core.db import db


async def next_seq(key: str) -> int:
    doc = await db.counters.find_one_and_update(
        {"key": key}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return int(doc["seq"])


async def next_ref(prefix: str, width: int = 6) -> str:
    """e.g. next_ref("HUO") -> HUO-2026-000123. The counter restarts every year."""
    year = datetime.now(timezone.utc).year
    n = await next_seq(f"{prefix}-{year}")
    return f"{prefix}-{year}-{n:0{width}d}"


def fi_reference(base: str | int) -> str:
    """Finnish national payment reference (viitenumero): base digits + 7-3-1 check digit."""
    digits = "".join(ch for ch in str(base) if ch.isdigit()).lstrip("0") or "1"
    if len(digits) < 3:
        digits = digits.rjust(3, "1")
    weights = [7, 3, 1]
    total = sum(int(d) * weights[i % 3] for i, d in enumerate(reversed(digits)))
    check = (10 - total % 10) % 10
    ref = digits + str(check)
    # grouped in fives from the right, as printed on Finnish invoices
    groups = []
    while ref:
        groups.insert(0, ref[-5:])
        ref = ref[:-5]
    return " ".join(groups)


def rf_reference(fi_ref: str) -> str:
    """International RF creditor reference (ISO 11649) built from a Finnish reference."""
    base = fi_ref.replace(" ", "")
    tmp = base + "RF00"
    num = "".join(str(int(c, 36)) for c in tmp)
    check = 98 - int(num) % 97
    return f"RF{check:02d}{base}"


def ytunnus_valid(value: str) -> bool:
    """Finnish business ID check digit (weights 7,9,10,5,8,4,2; mod 11)."""
    import re

    v = (value or "").strip()
    m = re.fullmatch(r"(\d{6,7})-(\d)", v)
    if not m:
        return False
    body = m.group(1).rjust(7, "0")
    weights = [7, 9, 10, 5, 8, 4, 2]
    rem = sum(int(d) * w for d, w in zip(body, weights)) % 11
    if rem == 1:
        return False
    check = 0 if rem == 0 else 11 - rem
    return check == int(m.group(2))


def ytunnus_to_vat(ytunnus: str) -> str:
    return "FI" + ytunnus.replace("-", "").rjust(8, "0")


def ytunnus_to_eori(ytunnus: str) -> str:
    """Finnish EORI = "FI" + Y-tunnus with the hyphen (FI1234567-8); EU EOS only validates this form.
    The number exists only after the company has applied for it at Tulli."""
    digits = ytunnus.replace("-", "").rjust(8, "0")
    return f"FI{digits[:7]}-{digits[7]}"
