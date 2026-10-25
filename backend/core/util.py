"""Small shared helpers: ids, timestamps, money, pagination."""
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def new_id() -> str:
    return str(uuid.uuid4())


# The application's clock. A demo world (services/demo_world/boundary.py, DEMO_WORLD_SANDBOX) may install a faster
# simulated clock here; `_rate` is then its speed (simulated seconds per real second).
_clock = None
_rate = None


def real_now() -> datetime:
    """Wall-clock time, for things that must follow the real world (TLS, provider rate limits)."""
    return datetime.now(timezone.utc)


def now() -> datetime:
    return _clock() if _clock is not None else datetime.now(timezone.utc)


def span(seconds: float) -> timedelta:
    """`seconds` of wall time expressed on now()'s clock: for operational timers (leases, polling, retries)."""
    return timedelta(seconds=float(seconds) * (_rate() if _rate is not None else 1.0))


def iso(dt: datetime | None) -> str | None:
    """UTC ISO-8601 with a Z suffix, second precision. Stored form of every timestamp."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now())


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.fromisoformat(s[:10])
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_from_now(days: float) -> str:
    return iso(now() + timedelta(days=days))


def cents(amount_eur: float | int | None) -> int:
    """EUR amount to integer cents. All money is stored as integer cents (fields *_cents)."""
    if amount_eur is None:
        return 0
    return int(round(float(amount_eur) * 100))


def eur(amount_cents: int | None) -> float:
    return round((amount_cents or 0) / 100, 2)


def clean(doc: dict | None) -> dict | None:
    """Drop Mongo's _id before returning a document from the API."""
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def pick(d: dict, *keys: str) -> dict[str, Any]:
    return {k: d.get(k) for k in keys if k in d}


NO_ID = {"_id": 0}


def flight_label(v: Any) -> str:
    """A leg's flight as text. Flight tracking stores {callsign, date, icao24}; mails and PDFs print the callsign."""
    if isinstance(v, dict):
        return str(v.get("callsign") or v.get("number") or v.get("flight") or "").strip()
    return str(v or "").strip()
