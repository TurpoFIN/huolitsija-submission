"""EU Binding Tariff Information (EBTI): official classification precedents for HS/CN codes.

Source: the European Commission's EBTI database (DG TAXUD), public downloads on
https://ec.europa.eu/taxation_customs/dds2/ebti/ — "Download All BTIs" (DDS2-EBTI_Full.zip, one CSV per year,
~400 MB) and the daily update files (EBTI_<yyyymmdd>_<hhmmss>.zip). Columns: BTI_REFERENCE, ISSUING_COUNTRY,
START/END_DATE_OF_VALIDITY, NOMENCLATURE_CODE, CLASSIFICATION_JUSTIFICATION, STATUS, LANGUAGE, PLACE_OF_ISSUE,
DATE_OF _ISSUE, NAME_AND_ADDRESS, DESCRIPTION_OF_GOODS, KEYWORDS.

Only BTIs that are VALID and not expired are kept (collection `ebti_rulings`, ~120 000 rulings). The holder's name
and address is never stored. A BTI binds only its holder, so here it is evidence, not a decision: the customs agent
counts "an official BTI classifies the same goods under the same CN code" as an independent second signal next to
the AI classification, and a BTI for the same goods under a different code as a reason to ask a person.

Jobs: `ebti.refresh` (daily 05:40 Helsinki: daily update files since the last run; a full reload when the
collection is empty or older than EBTI_FULL_RELOAD_DAYS). EBTI_ENABLED=false turns the whole module off.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import tempfile
import time
import uuid
import zipfile
from datetime import date, datetime

import httpx

from core import config
from core.db import db, register_index
from core.util import NO_ID, now_iso

log = logging.getLogger("ebti")

BASE = "https://ec.europa.eu/taxation_customs/dds2/ebti"
FULL_URL = f"{BASE}/ebti_export_management.jsp?message=extractFull"
DAILY_PAGE = f"{BASE}/daily_publications.jsp?Lang=en"
DETAILS_URL = f"{BASE}/ebti_details.jsp?Lang=en&reference={{ref}}"
SOURCE = "EU EBTI database (European Commission, DG TAXUD)"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 Huolitsija"
ENABLED = config.env_bool("EBTI_ENABLED", True)
FULL_RELOAD_DAYS = int(config.env("EBTI_FULL_RELOAD_DAYS", "30"))
DESC_MAX = 700

register_index("ebti_rulings", "ref", unique=True)
register_index("ebti_rulings", "code")
register_index("ebti_rulings", "valid_to")


def _date(v: str | None) -> str | None:
    v = (v or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):  # full export / daily files
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _code(v: str | None) -> str:
    """'8712003000**********' -> '8712003000' (the export pads the 10-digit TARIC code with asterisks)."""
    return re.sub(r"\D", "", (v or "").split("*")[0])[:10]


def row_doc(row: dict) -> dict | None:
    """One CSV row -> the stored ruling (None for rows without a reference or code). Holder data is dropped."""
    ref = (row.get("BTI_REFERENCE") or "").strip()
    code = _code(row.get("NOMENCLATURE_CODE"))
    if not ref or len(code) < 6:
        return None
    return {"ref": ref, "country": (row.get("ISSUING_COUNTRY") or "").strip(), "code": code,
            "valid_from": _date(row.get("START_DATE_OF_VALIDITY")), "valid_to": _date(row.get("END_DATE_OF_VALIDITY")),
            "status": (row.get("STATUS") or "").strip().upper(), "lang": (row.get("LANGUAGE") or "").strip().lower(),
            "issued": _date(row.get("DATE_OF _ISSUE") or row.get("DATE_OF_ISSUE")),
            "description": re.sub(r"\s+", " ", row.get("DESCRIPTION_OF_GOODS") or "").strip()[:DESC_MAX],
            "keywords": [k.strip() for k in re.split(r"[\n;]+", row.get("KEYWORDS") or "") if k.strip()][:20],
            "justification": re.sub(r"\s+", " ", row.get("CLASSIFICATION_JUSTIFICATION") or "").strip()[:300]}


def is_current(doc: dict, on: str | None = None) -> bool:
    on = on or date.today().isoformat()
    return doc.get("status") == "VALID" and bool(doc.get("valid_to")) and doc["valid_to"] >= on and \
        (not doc.get("valid_from") or doc["valid_from"] <= on)


def iter_zip(source) -> "iter[dict]":
    """Rows of every CSV inside an EBTI zip (path or bytes)."""
    csv.field_size_limit(10 ** 9)
    zf = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        with zf.open(name) as fh:
            yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))


async def load_rows(rows, *, on: str | None = None) -> dict:
    """Upsert current rulings, delete rulings that are no longer valid (invalidated, revoked or expired)."""
    from pymongo import DeleteOne, ReplaceOne
    ops, stats = [], {"upserted": 0, "removed": 0, "rows": 0}
    fetched = now_iso()

    async def flush():
        if ops:
            res = await db.ebti_rulings.bulk_write(list(ops), ordered=False)
            stats["upserted"] += res.upserted_count + res.modified_count
            stats["removed"] += res.deleted_count
            ops.clear()

    for row in rows:
        stats["rows"] += 1
        d = row_doc(row)
        if not d:
            continue
        if is_current(d, on):
            ops.append(ReplaceOne({"ref": d["ref"]}, {**d, "fetched_at": fetched}, upsert=True))
        else:
            ops.append(DeleteOne({"ref": d["ref"]}))
        if len(ops) >= 2000:
            await flush()
    await flush()
    return stats


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, timeout=httpx.Timeout(60, read=600))


async def daily_files() -> list[dict]:
    """[{published: '2026-10-24 04:41:15', file: 'EBTI_20261024_044115.zip', url}] newest first."""
    async with await _client() as c:
        r = await c.get(DAILY_PAGE)
        r.raise_for_status()
    out = []
    for m in re.finditer(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*</td>\s*<td[^>]*>\s*(EBTI_\d{8}_\d{6}\.zip)", r.text):
        out.append({"published": m.group(1), "file": m.group(2),
                    "url": f"{BASE}/ebti_export_management.jsp?publicationDate={m.group(1)}&message=extract"})
    return out


async def _download(url: str, dest) -> int:
    async with await _client() as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            if "zip" not in (r.headers.get("content-type") or "") and not str(r.url).endswith(".zip"):
                # the full export answers with a page that links the prepared zip file
                body = (await r.aread()).decode("utf-8", "replace")
                m = re.search(r'href="(https://[^"]*downloadcsv\?[^"]*\.zip[^"]*)"', body)
                if not m:
                    raise RuntimeError(f"EBTI download page had no zip link ({url})")
                return await _download(m.group(1).replace("&amp;", "&"), dest)
            n = 0
            async for chunk in r.aiter_bytes(1 << 20):
                dest.write(chunk)
                n += len(chunk)
    dest.flush()
    return n


STAGING = "ebti_rulings_rebuild"
STAGING_ORPHAN_SECONDS = 2 * 3600


async def rebuild(rows, *, on: str | None = None) -> dict:
    """Full reload: current rulings only into a fresh collection, indexed, then swapped in with one rename (readers
    never see a half-loaded index). ~1 M rows in, ~120 000 kept."""
    raw = db.raw
    # Each run builds its own staging collection: a run killed by a redeploy keeps its lease for an hour, and its
    # takeover must not drop the staging data of a run that is still loading (that could swap in a partial set).
    started = int(time.time())
    tmp_name = f"{STAGING}_{started}_{uuid.uuid4().hex[:6]}"
    for name in await raw.list_collection_names(filter={"name": {"$regex": f"^{STAGING}"}}):
        stamp = re.match(rf"^{STAGING}_(\d+)_", name)
        if not stamp or started - int(stamp.group(1)) > STAGING_ORPHAN_SECONDS:
            await raw[name].drop()  # left by a run that was killed (older than any job's timeout)
    fetched, batch, stats, seen = now_iso(), [], {"rows": 0, "kept": 0}, set()
    for row in rows:
        stats["rows"] += 1
        d = row_doc(row)
        if not d or not is_current(d, on) or d["ref"] in seen:
            continue
        seen.add(d["ref"])
        batch.append({**d, "fetched_at": fetched})
        if len(batch) >= 5000:
            await raw[tmp_name].insert_many(batch, ordered=False)
            stats["kept"] += len(batch)
            batch = []
    if batch:
        await raw[tmp_name].insert_many(batch, ordered=False)
        stats["kept"] += len(batch)
    await raw[tmp_name].create_index("ref", unique=True)
    await raw[tmp_name].create_index("code")
    await raw[tmp_name].create_index("valid_to")
    await raw[tmp_name].rename("ebti_rulings", dropTarget=True)
    return stats


async def refresh_full() -> dict:
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        size = await _download(FULL_URL, tmp)
        stats = await rebuild(iter_zip(tmp.name))
    meta = {"key": "ebti", "full_at": now_iso(), "full_bytes": size, "count": await db.ebti_rulings.count_documents({}),
            "source": SOURCE, "url": FULL_URL}
    await db.system.update_one({"key": "ebti"}, {"$set": meta}, upsert=True)
    log.info("EBTI full reload: %s", stats)
    return {**stats, **meta}


async def refresh_daily(since: str | None = None) -> dict:
    meta = await db.system.find_one({"key": "ebti"}, NO_ID) or {}
    since = since or meta.get("daily_published") or meta.get("full_at", "")[:10]
    files = [f for f in await daily_files() if f["published"] > (since or "")]
    total = {"files": [], "upserted": 0, "removed": 0}
    for f in sorted(files, key=lambda x: x["published"]):
        buf = io.BytesIO()
        await _download(f["url"], buf)
        s = await load_rows(iter_zip(buf.getvalue()))
        total["files"].append(f["file"])
        total["upserted"] += s["upserted"]
        total["removed"] += s["removed"]
        await db.system.update_one({"key": "ebti"}, {"$set": {"daily_published": f["published"], "daily_at": now_iso()}},
                                   upsert=True)
    total["expired_removed"] = (await db.ebti_rulings.delete_many({"valid_to": {"$lt": date.today().isoformat()}})).deleted_count
    return total


def _public(d: dict) -> dict:
    return {k: d.get(k) for k in ("ref", "country", "code", "valid_from", "valid_to", "lang", "description", "keywords",
                                  "justification")} | {"url": DETAILS_URL.format(ref=d.get("ref"))}


async def by_code(code: str, *, limit: int = 20, on: str | None = None) -> dict:
    """Current BTIs whose 10-digit code starts with `code` (HS6, CN8 or TARIC10)."""
    digits = re.sub(r"\D", "", code or "")[:10]
    if len(digits) < 4:
        return {"code": digits, "count": 0, "rulings": [], "source": SOURCE}
    on = on or date.today().isoformat()
    q = {"code": {"$regex": f"^{digits}"}, "status": "VALID", "valid_to": {"$gte": on}}
    n = await db.ebti_rulings.count_documents(q)
    rows = await db.ebti_rulings.find(q, NO_ID).sort("valid_from", -1).limit(max(1, min(limit, 200))).to_list(None)
    return {"code": digits, "count": n, "rulings": [_public(r) for r in rows], "source": SOURCE}


async def get(ref: str) -> dict | None:
    d = await db.ebti_rulings.find_one({"ref": ref}, NO_ID)
    return _public(d) if d else None


async def status() -> dict:
    meta = await db.system.find_one({"key": "ebti"}, NO_ID) or {}
    return {"enabled": ENABLED, "count": await db.ebti_rulings.estimated_document_count(), "full_at": meta.get("full_at"),
            "daily_published": meta.get("daily_published"), "source": SOURCE}


# ---------------------------------------------------------------------------------------------
# Precedent check for a classified item
# ---------------------------------------------------------------------------------------------
JUDGE_SYSTEM = """You compare goods with official EU Binding Tariff Information (BTI) rulings.
A ruling describes goods in the issuing country's language. Decide whether one ruling covers the SAME kind of goods as
the item (same product type, material and function; brand and model do not matter). If none does, answer null.
Answer JSON: {"ref": "<BTI reference or null>", "why": "<one short English sentence>", "why_fi": "<sama suomeksi>"}"""


async def precedent(item: dict, codes: list[str], *, per_code: int = 6, ref: dict | None = None,
                    company_id: str | None = None) -> dict:
    """Official BTI precedents for an item and its candidate codes.

    -> {checked_at, source, codes: {code: count}, shown: [refs], match: {ref, code, country, valid_to, url, why, why_fi,
        agrees_with: code|None} | None, status: "match" | "no_match" | "no_rulings" | "no_model" | "error"}
    `match` comes from the model choosing among rulings we fetched; a reference we did not show is discarded.
    """
    from core import llm
    out: dict = {"checked_at": now_iso(), "source": SOURCE, "codes": {}, "shown": [], "match": None}
    cands = []
    seen = set()
    for c in codes:
        d = re.sub(r"\D", "", c or "")[:8]
        if len(d) >= 6 and d not in seen:
            seen.add(d)
            cands.append(d)
    shown: dict[str, dict] = {}
    for c in cands[:3]:
        res = await by_code(c, limit=60)
        out["codes"][c] = res["count"]
        pick = sorted(res["rulings"], key=lambda r: (r["lang"] not in ("en", "de", "fr", "sv", "fi"), r["lang"] != "en"))
        for r in pick[:per_code]:
            shown[r["ref"]] = r
    out["shown"] = list(shown)
    if not shown:
        out["status"] = "no_rulings"
        return out
    if not llm.llm_available():
        out["status"] = "no_model"
        return out
    lines = "\n".join(f"- {r['ref']} | code {r['code']} | {r['country']} | {r['lang']} | {r['description'][:500]}"
                      for r in shown.values())
    prompt = (f"Item: {item.get('description')}\nOrigin: {item.get('origin_country') or '-'}\n\nRulings:\n{lines}\n\n"
              "Which ruling covers the same goods?")
    try:
        ans = await llm.llm_json("ebti_precedent", JUDGE_SYSTEM, prompt, max_tokens=400, ref=ref, company_id=company_id)
    except Exception as exc:  # noqa: BLE001 - no model answer = no signal, never a guess
        out.update(status="error", error=str(exc)[:200])
        return out
    r = shown.get((ans or {}).get("ref") or "")
    if not r:
        out["status"] = "no_match"
        return out
    out["match"] = {"ref": r["ref"], "code": r["code"], "country": r["country"], "valid_to": r["valid_to"], "url": r["url"],
                    "why": (ans.get("why") or "")[:300], "why_fi": (ans.get("why_fi") or "")[:300]}
    out["status"] = "match"
    return out


def agreement(it: dict) -> tuple[str, str] | None:
    """('agrees'|'disagrees', reason) for an item whose hs_suggestion carries an EBTI precedent, else None."""
    p = ((it.get("hs_suggestion") or {}).get("ebti") or {}).get("match")
    cn = re.sub(r"\D", "", it.get("cn_code") or "")
    if not p or len(cn) < 8:
        return None
    if p["code"][:8] == cn[:8]:
        return "agrees", f"official BTI {p['ref']} ({p['country']}) classifies the same goods under {p['code'][:8]}"
    return "disagrees", f"official BTI {p['ref']} ({p['country']}) classifies the same goods under {p['code'][:8]}, not {cn[:8]}"


async def annotate(items: list[dict], results: list[dict], *, ref: dict | None = None, company_id: str | None = None):
    """Adds results[i]["ebti"] for classified items (called by services.hs.classify_items)."""
    if not ENABLED or not await db.ebti_rulings.estimated_document_count():
        return
    for it, res in zip(items, results):
        if not (res.get("cn8") or res.get("hs6")):
            continue
        codes = [res.get("cn8") or res.get("hs6")] + [c.get("code") or "" for c in (res.get("candidates") or [])[:2]]
        if not any(codes):
            continue
        try:
            res["ebti"] = await precedent({"description": (it or {}).get("description"),
                                           "origin_country": (it or {}).get("origin_country")}, codes,
                                          ref=ref, company_id=company_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("EBTI precedent check failed: %s", exc)


# ---------------------------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------------------------
from core.jobs import job, schedule  # noqa: E402


@job("ebti.refresh", max_attempts=4, backoff=(600, 1800, 3600, 7200), timeout=3600,
     label_fi="BTI-ratkaisujen päivitys (EU EBTI)", label_en="BTI rulings update (EU EBTI)")
async def ebti_refresh(j):
    if not ENABLED:
        return {"skipped": "EBTI_ENABLED=false"}
    meta = await db.system.find_one({"key": "ebti"}, NO_ID) or {}
    full_due = (not await db.ebti_rulings.estimated_document_count() or not meta.get("full_at")
                or (datetime.fromisoformat(meta["full_at"].replace("Z", "+00:00")).date() - date.today()).days
                <= -FULL_RELOAD_DAYS or (j.get("payload") or {}).get("full"))
    if full_due:
        return {"full": await refresh_full()}
    return {"daily": await refresh_daily()}


schedule("ebti.daily", job="ebti.refresh", daily_at="05:40", label_fi="BTI-ratkaisut (EU EBTI)",
         label_en="BTI rulings (EU EBTI)")()


from core.lifecycle import on_startup  # noqa: E402


@on_startup(order=90)
async def ebti_initial_load():
    """A new database gets the full BTI set from the first worker run, not from tomorrow's 05:40 schedule."""
    if not ENABLED or await db.ebti_rulings.estimated_document_count():
        return
    from core.jobs import enqueue
    await enqueue("ebti.refresh", {"full": True}, key="ebti:initial-load", source="startup")
