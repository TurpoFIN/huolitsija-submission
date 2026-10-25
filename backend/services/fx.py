"""Exchange rates for accounting: ECB euro foreign exchange reference rates (daily, published ~16:00 CET on TARGET days).

Rates are stored in `fx_rates` {date, rates{CUR: units per 1 EUR}, source: "ECB"} — the same collection the customs
valuation uses (services.customs.customs_rate applies the customs-month rule on top of it). For a booking date the
accounting rate is the latest ECB reference rate on or before that date (weekends and TARGET holidays use the previous
publication), which is also the rate Finnish VAT law allows for converting foreign-currency invoice amounts (AVL 73 §,
VAT Directive art. 91(2): the latest ECB rate).

Sources (no key needed): eurofxref-daily.xml (today), eurofxref-hist-90d.xml (90 days), eurofxref-hist.zip (since 1999).
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from xml.etree import ElementTree as ET

import httpx

from core.db import db, register_index
from core.util import NO_ID, now_iso

log = logging.getLogger("fx")

ECB_DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_90D = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_HIST = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
SOURCE = "ECB euro foreign exchange reference rate"

register_index("fx_rates", "date", unique=True)


class FxUnavailable(Exception):
    """No ECB rate for this currency on or before the date (unknown currency or the feed could not be read)."""


def _parse_xml(content: bytes) -> list[tuple[str, dict]]:
    root = ET.fromstring(content)
    out = []
    for cube in root.iter():
        if cube.tag.endswith("Cube") and cube.get("time"):
            rates = {c.get("currency"): float(c.get("rate")) for c in cube if c.get("currency")}
            if rates:
                out.append((cube.get("time"), rates))
    return out


def _parse_hist_zip(content: bytes, since: str | None = None) -> list[tuple[str, dict]]:
    zf = zipfile.ZipFile(io.BytesIO(content))
    name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
    rows = csv.reader(io.StringIO(zf.read(name).decode("utf-8")))
    header = [h.strip() for h in next(rows)]
    out = []
    for row in rows:
        if not row or not row[0].strip():
            continue
        day = row[0].strip()
        if since and day < since:
            continue
        rates = {}
        for cur, val in zip(header[1:], row[1:]):
            val = val.strip()
            if cur and val and val != "N/A":
                try:
                    rates[cur] = float(val)
                except ValueError:
                    continue
        if rates:
            out.append((day, rates))
    return out


async def _store(days: list[tuple[str, dict]], feed: str) -> int:
    n = 0
    for day, rates in days:
        res = await db.fx_rates.update_one({"date": day}, {"$set": {"rates": rates, "source": "ECB", "feed": feed,
                                                                    "fetched_at": now_iso()}}, upsert=True)
        n += 1 if (res.upserted_id is not None or res.modified_count) else 0
    return n


async def refresh(kind: str = "daily", since: str | None = None) -> dict:
    """Fetch ECB rates. kind: daily | 90d | hist (hist stores days >= since, default 3 years back)."""
    url = {"daily": ECB_DAILY, "90d": ECB_90D, "hist": ECB_HIST}[kind]
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    if kind == "hist":
        days = _parse_hist_zip(r.content, since or (date.today() - timedelta(days=3 * 366)).isoformat())
    else:
        days = _parse_xml(r.content)
    changed = await _store(days, kind)
    latest = max((d for d, _ in days), default=None)
    await db.system.update_one({"key": "fx_refresh"}, {"$set": {"at": now_iso(), "kind": kind, "latest": latest,
                                                                "days": len(days), "changed": changed}}, upsert=True)
    log.info("ECB %s: %s days, latest %s", kind, len(days), latest)
    return {"kind": kind, "days": len(days), "changed": changed, "latest": latest, "url": url}


async def _rate_doc(on: str) -> dict | None:
    return await db.fx_rates.find_one({"date": {"$lte": on}}, NO_ID, sort=[("date", -1)])


async def eur_rate(currency: str, on: str | date | None = None, *, fetch: bool = True) -> dict:
    """Units of `currency` per 1 EUR from the latest ECB publication on or before `on` (YYYY-MM-DD, default today).

    Fetches the 90-day (or full) history when the stored rates do not reach back to `on`."""
    currency = (currency or "EUR").upper()
    on_s = (on.isoformat() if isinstance(on, date) else (on or now_iso()))[:10]
    if currency == "EUR":
        return {"currency": "EUR", "rate": 1.0, "rate_date": on_s, "source": "EUR"}
    doc = await _rate_doc(on_s)
    stale = doc is None or currency not in (doc.get("rates") or {}) or \
        (date.fromisoformat(on_s) - date.fromisoformat(doc["date"])).days > 4
    if stale and fetch:
        age = (date.today() - date.fromisoformat(on_s)).days
        try:
            await refresh("daily" if age <= 1 else "90d" if age < 85 else "hist",
                          since=(date.fromisoformat(on_s) - timedelta(days=10)).isoformat())
        except Exception as exc:  # noqa: BLE001 - surfaced as FxUnavailable below when nothing usable is stored
            log.warning("ECB fetch failed: %s", exc)
        doc = await _rate_doc(on_s)
    if not doc or currency not in (doc.get("rates") or {}):
        raise FxUnavailable(f"No ECB reference rate for {currency} on or before {on_s}")
    return {"currency": currency, "rate": float(doc["rates"][currency]), "rate_date": doc["date"], "source": SOURCE}


def convert(amount_cents: int, rate: float) -> int:
    """Foreign cents -> EUR cents at `rate` (units per EUR), half-up."""
    return int((Decimal(int(amount_cents)) / Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_eur(eur_cents: int, rate: float) -> int:
    """EUR cents -> foreign cents at `rate` (units per EUR), half-up."""
    return int((Decimal(int(eur_cents)) * Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def to_eur(amount_cents: int, currency: str, on: str | date | None = None) -> tuple[int, dict]:
    fx = await eur_rate(currency, on)
    return (int(amount_cents) if fx["currency"] == "EUR" else convert(amount_cents, fx["rate"])), fx


def realised_difference(booked_eur_cents: int, settled_eur_cents: int, *, side: str) -> int:
    """Realised FX result in EUR cents, positive = gain. side 'ar': we receive foreign currency (more EUR = gain);
    side 'ap': we pay foreign currency (fewer EUR = gain)."""
    diff = int(settled_eur_cents) - int(booked_eur_cents)
    return diff if side == "ar" else -diff


async def latest_rates() -> dict:
    doc = await db.fx_rates.find_one({}, NO_ID, sort=[("date", -1)])
    meta = await db.system.find_one({"key": "fx_refresh"}, {"_id": 0}) or {}
    return {"date": (doc or {}).get("date"), "rates": (doc or {}).get("rates") or {}, "source": SOURCE,
            "fetched_at": meta.get("at"), "feed": meta.get("kind")}
