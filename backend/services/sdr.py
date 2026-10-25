"""SDR (special drawing right) to EUR rate from the IMF, cached daily, with a dated fallback.

Source: IMF "Representative exchange rates for selected currencies", TSV export of the last five days,
section "Currency units per SDR", row "Euro". The newest non-empty column wins and its date is kept, so a
liability amount in EUR always states the rate date it was converted at (RULE N-3).
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from core.db import db, register_index
from core.lifecycle import background_loop
from core.util import NO_ID, now, now_iso, parse_iso

log = logging.getLogger("sdr")
IMF_TSV = "https://www.imf.org/external/np/fin/data/rms_five.aspx?tsvflag=Y"
IMF_PAGE = "https://www.imf.org/external/np/fin/data/rms_five.aspx"

register_index("sdr_rates", "date", unique=True)


def parse_imf_tsv(text: str) -> tuple[str, float]:
    """Returns (ISO date, EUR per SDR) from the IMF five-day TSV."""
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip().lower().startswith("currency units per sdr"))
    except StopIteration:
        raise ValueError("IMF file has no 'Currency units per SDR' section")
    header = None
    for ln in lines[start + 1:]:
        cells = ln.split("\t")
        if cells and cells[0].strip().lower() == "currency":
            header = cells
            continue
        if header and cells and cells[0].strip().lower() == "euro":
            for col in range(1, len(cells)):
                val = cells[col].strip()
                if not val:
                    continue
                d = datetime.strptime(header[col].strip(), "%B %d, %Y").date()
                rate = float(val)
                if not 0.5 < rate < 3:
                    raise ValueError(f"Implausible EUR per SDR value {rate}")
                return d.isoformat(), rate
            break
    raise ValueError("IMF file has no Euro rate")


async def fetch_imf() -> dict:
    # The IMF CDN rejects some user agents (403); try ours, then the client default.
    last_exc: Exception | None = None
    for headers in ({"User-Agent": "Huolitsija/1.0 (+https://huolitsija.fi)"}, {}):
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as c:
                r = await c.get(IMF_TSV)
                r.raise_for_status()
            break
        except httpx.HTTPError as exc:
            last_exc = exc
    else:
        raise last_exc or RuntimeError("IMF not reachable")
    d, rate = parse_imf_tsv(r.text)
    doc = {"date": d, "eur_per_sdr": rate, "source": "IMF representative rates (Currency units per SDR)",
           "source_url": IMF_PAGE, "fetched_at": now_iso()}
    await db.sdr_rates.update_one({"date": d}, {"$set": doc}, upsert=True)
    return doc


async def get_rate(max_age_hours: float = 12) -> dict | None:
    """Latest stored rate; refreshes from the IMF when older than max_age_hours. Falls back to the last stored rate
    (marked stale, with its date) when the IMF cannot be reached. None if no rate was ever fetched."""
    last = await db.sdr_rates.find_one({}, NO_ID, sort=[("date", -1)])
    fresh = last and (now() - parse_iso(last["fetched_at"])).total_seconds() < max_age_hours * 3600
    if fresh:
        return {**last, "stale": False}
    try:
        doc = await fetch_imf()
        return {**doc, "stale": False}
    except Exception as exc:  # noqa: BLE001
        log.warning("IMF SDR fetch failed: %s", exc)
        if last:
            return {**last, "stale": True, "error": f"IMF not reachable; using the rate of {last['date']}"}
        return None


@background_loop(seconds=6 * 3600)
async def sdr_refresh_loop():
    await get_rate(max_age_hours=12)
