"""DG TAXUD CS/RD2 COL snapshots; no inferred offices, roles or coordinates.

The generic export is a dated snapshot, not a history of all office versions.
activeFrom/activeTo belong to the office version; absent activeTo is open ended.
Role/traffic competence is scoped to the published timetable season.
"""
import asyncio
import hashlib
import io
import itertools
import math
import os
import re
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from lxml import etree

from core.db import db
from core.jobs import job, schedule

BASE = "https://ec.europa.eu/taxation_customs/dds2/rd/"
DOWNLOAD_PAGE = BASE + "rd_download_home.jsp?Lang=en"
USER_AGENT = "Mozilla/5.0 (compatible; Huolitsija-customs/1.0; +https://huolitsija.fi)"
SOURCE_HOSTS = {"ec.europa.eu", "taxation-customs.ec.europa.eu", "circabc.europa.eu"}
MAX_AGE_SECONDS = 48 * 3600
MAX_DOWNLOAD_BYTES = 50_000_000  # the ZIP is ~2 MB; the XML inside is ~60 MB
INSERT_BATCH = 100  # a row with its timetables is ~50 KB: 100 rows plus their BSON copy stay ~10 MB
MODES = {"sea": "P", "air": "AIR", "road": "R", "rail": "V", "inland_waterway": "C",
         "mail": "5", "fixed": "PP"}
NS = {"entry": "http://xmlns.ec.eu/BusinessObjects/CSRD2/RDEntryType/V2",
      "status": "http://xmlns.ec.eu/BusinessObjects/CSRD2/RDStatusType/V2"}


class OfficeDataUnavailable(ValueError):
    pass


def _day(value=None):
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, datetime):
        value = value.date()
    return date.fromisoformat(str(value)).isoformat()


def _date(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y%m%d").date().isoformat() if len(value) == 8 else _day(value)


def _items(node):
    return {e.get("name"): e.text for e in node.findall("entry:dataItem", NS)}


def _groups(node, name):
    return node.findall(f'entry:dataGroup[@name="{name}"]', NS)


def parse_col(raw):
    """Parse the official COL-Generic ZIP (or XML) into a list; reject HTML/empty/duplicate snapshots."""
    return list(iter_col(raw))


def iter_col(source):
    """Yield the COL offices one row at a time from bytes or a file path (ZIP or XML).

    The XML is ~60 MB; a whole-document tree took ~620 MB RSS and got the live container killed mid-refresh.
    Each RDEntry is read, turned into a row and freed before the next one, and the ZIP member is decompressed as
    it is read, so memory stays flat however large the list grows.
    """
    if isinstance(source, (bytes, bytearray)):
        if bytes(source[:2]) == b"PK":
            with zipfile.ZipFile(io.BytesIO(source)) as archive:
                yield from _iter_archive(archive)
        else:
            yield from _iter_stream(io.BytesIO(source))
        return
    with open(source, "rb") as fh:
        head = fh.read(2)
        fh.seek(0)
        if head == b"PK":
            with zipfile.ZipFile(fh) as archive:
                yield from _iter_archive(archive)
        else:
            yield from _iter_stream(fh)


def _iter_archive(archive):
    names = [n for n in archive.namelist() if re.fullmatch(r"COL-Generic-\d{8}\.xml", n)]
    if len(names) != 1 or archive.getinfo(names[0]).file_size > 150_000_000:
        raise ValueError("Invalid COL archive members/size")
    with archive.open(names[0]) as stream:
        yield from _iter_stream(stream)


def _iter_stream(stream):
    entity_name, offices_seen, codes = None, False, set()
    context = etree.iterparse(stream, events=("start", "end"), resolve_entities=False, no_network=True,
                              load_dtd=False, huge_tree=False)
    depth = 0
    for event, node in context:
        if event == "start":
            depth += 1
            if depth == 1 and (etree.QName(node).localname != "RDEntityList" or
                               node.getroottree().docinfo.doctype):
                raise ValueError("Expected CS/RD2 COL RDEntityList")
            if depth == 2:
                entity_name = node.get("name")
                offices_seen = offices_seen or entity_name == "CustomsOffices"
            continue
        depth -= 1
        row = None
        if depth == 2 and entity_name == "CustomsOffices":
            row = _office_row(node)
            if row["code"] in codes:
                raise ValueError("Duplicate COL office versions")
            codes.add(row["code"])
        if depth == 2 or (depth == 1 and entity_name != "CustomsOffices"):
            parent = node.getparent()
            node.clear()
            while node.getprevious() is not None:
                del parent[0]
        if row is not None:
            yield row
    if not offices_seen or not codes:
        raise ValueError("COL contains no CustomsOffices")


def _office_row(entry):
    fields = _items(entry)
    code = fields.get("ReferenceNumber", "")
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{6}", code):
        raise ValueError("Invalid COL reference number")
    status = entry.find("entry:RDEntryStatus", NS)
    if status is None:
        raise ValueError("COL entry status missing")
    state = status.findtext("status:state", namespaces=NS)
    start = _date(status.findtext("status:activeFrom", namespaces=NS))
    end = _date(status.findtext("status:activeTo", namespaces=NS))
    if not state or not start:
        raise ValueError("COL validity missing")
    names = {_items(g)["LanguageCode"].lower(): _items(g) for g in _groups(entry, "CustomsOfficeLsd")}
    preferred = next((names[l] for l in ("fi", "en") if l in names), next(iter(names.values()), {}))
    competence = []
    timetables = []
    for table in _groups(entry, "CustomsOfficeTimetable"):
        season = _items(table)
        lines = []
        for line in _groups(table, "CustomsOfficeTimetableLine"):
            pairs = [_items(g) for g in _groups(line, "CustomsOfficeRoleTrafficCompetence")]
            lines.append({**_items(line), "competence": pairs})
            for pair in pairs:
                item = {"role": pair["Role"], "traffic_type": pair.get("TrafficType"),
                        "valid_from": _date(season.get("SeasonStartDate")),
                        "valid_to": _date(season.get("SeasonEndDate"))}
                if item not in competence:
                    competence.append(item)
        timetables.append({**season, "lines": lines})
    country = fields["CountryCode"]
    unlocode = fields.get("UnLocodeId")
    row = {"code": code, "reference_number": code, "country": country,
           "name": preferred.get("CustomsOfficeUsualName"), "translations": names,
           "address": {"street": preferred.get("StreetAndNumber"), "city": preferred.get("City"),
                       "postal_code": fields.get("PostalCode"), "country": country},
           "unlocode": country + unlocode if unlocode else None,
           "state": state, "valid_from": start, "valid_to": end,
           "roles": sorted({c["role"] for c in competence}), "competence": competence,
           "timetables": timetables, "source_fields": fields}
    # GeoInfoCode is a frontier/type code, NEVER latitude/longitude.
    if fields.get("Latitude") and fields.get("Longitude"):
        lat, lon = float(fields["Latitude"]), float(fields["Longitude"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid COL coordinates")
        row.update(lat=lat, lon=lon)
    return row


class _NotPublished(Exception):
    """The Commission answers 200 with an empty body for a day whose COL file is not out yet."""


async def download(on=None, *, client=None, attempts=3, lookback=2):
    """(raw bytes, rows, provenance) for the capture script and tests. refresh() never holds these in memory: it
    uses download_to_file() and install_snapshot(path) instead."""
    path, provenance = await download_to_file(on, client=client, attempts=attempts, lookback=lookback)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    finally:
        os.unlink(path)
    rows = await asyncio.to_thread(parse_col, raw)
    return raw, rows, {**provenance, "office_count": len(rows)}


async def download_to_file(on=None, *, client=None, attempts=3, lookback=2):
    """Stream the official Reference Data distribution to a temporary file; bounded exponential backoff on failures.

    Returns (path, provenance); the caller deletes the file. The body is written in 1 MB chunks and hashed as it
    arrives, so the download is never held in memory whole. The day's file appears some hours into the UTC day
    (until then the URL answers 200 with 0 bytes), so an empty answer falls back to the newest earlier day, at most
    `lookback` days back (fresh() accepts a 2-day-old snapshot).
    """
    first = date.fromisoformat(_day(on))
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True,
                                        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        empty = []
        for back in range(lookback + 1):
            day = (first - timedelta(days=back)).isoformat()
            try:
                return await _download_day(client, day, attempts)
            except _NotPublished:
                empty.append(day)
        raise OfficeDataUnavailable(f"COL file not published (empty) for {', '.join(empty)}")
    finally:
        if own:
            await client.aclose()


def _check_download(path):
    """Cheap shape check before the snapshot is installed: a COL ZIP with one bounded member, or COL XML."""
    with open(path, "rb") as fh:
        head = fh.read(512)
    if head[:2] == b"PK":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if re.fullmatch(r"COL-Generic-\d{8}\.xml", n)]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 150_000_000:
                raise ValueError("Invalid COL archive members/size")
    elif b"RDEntityList" not in head:
        raise ValueError("Expected CS/RD2 COL RDEntityList")


async def _download_day(client, day, attempts):
    url = BASE + "compressed_file/data_download/COL-Generic-" + day.replace("-", "") + ".zip"
    for attempt in range(attempts):
        fd, path = tempfile.mkstemp(prefix="col-", suffix=".download")
        keep = False
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as fh:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    if urlparse(str(response.url)).hostname not in SOURCE_HOSTS:
                        raise ValueError("COL redirected outside official distribution")
                    async for chunk in response.aiter_bytes(1 << 20):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("COL download larger than expected")
                        digest.update(chunk)
                        fh.write(chunk)
                    source_url = str(response.url)
            if not size:
                raise _NotPublished(day)
            await asyncio.to_thread(_check_download, path)
            keep = True
            return path, {"source": "EU COL (DG TAXUD CS/RD2)", "source_url": source_url, "source_date": day,
                          "fetched_at": datetime.now(timezone.utc).isoformat(), "sha256": digest.hexdigest(),
                          "size_bytes": size}
        except (httpx.HTTPError, ValueError, zipfile.BadZipFile) as exc:
            if attempt + 1 == attempts:
                raise OfficeDataUnavailable(f"COL download failed after {attempts} attempts: {exc}") from exc
            await asyncio.sleep(2 ** attempt)
        finally:
            if not keep:
                os.unlink(path)


def _sha256_of(source):
    if isinstance(source, (bytes, bytearray)):
        return hashlib.sha256(source).hexdigest()
    digest = hashlib.sha256()
    with open(source, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _next_batch(rows, size=INSERT_BATCH):
    return list(itertools.islice(rows, size))


async def install_snapshot(source, provenance, rows=None):
    """Atomic replacement: readers see a whole snapshot, never a partly refreshed list.

    `source` is the downloaded ZIP/XML as bytes or a file path. Rows are parsed from it as they are inserted, 100 at
    a time, so neither the XML nor the full row list is ever in memory (`rows` only for callers that already hold
    them). Loading a captured fixture preserves its original capture date (no freshness laundering).
    """
    if (urlparse(provenance.get("source_url", "")).hostname not in SOURCE_HOSTS or
            provenance.get("sha256") != await asyncio.to_thread(_sha256_of, source)):
        raise ValueError("COL provenance/hash mismatch")
    _day(provenance["source_date"])
    datetime.fromisoformat(provenance["fetched_at"])
    stream = iter(rows) if rows is not None else iter_col(source)
    temporary = db.raw["customs_offices_loading_" + uuid.uuid4().hex]
    count = 0
    try:
        while True:
            batch = await asyncio.to_thread(_next_batch, stream)  # the parse runs off the event loop
            if not batch:
                break
            await temporary.insert_many([{**r, **provenance} for r in batch])
            count += len(batch)
            del batch
        if not count:
            raise ValueError("COL contains no CustomsOffices")
        await temporary.create_index("reference_number", unique=True)
        await temporary.create_index([("country", 1), ("roles", 1), ("unlocode", 1)])
        await temporary.update_many({}, {"$set": {"office_count": count}})
        await temporary.rename("customs_offices", dropTarget=True)
    except BaseException:
        await temporary.drop()
        raise
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    return {**provenance, "office_count": count}


async def refresh():
    """Download to a temporary file, then stream-parse it into a fresh collection 100 rows at a time."""
    from services.agents.base import entity_lock
    async with entity_lock("customs-offices-refresh"):
        path, provenance = await download_to_file()
        try:
            return await install_snapshot(path, provenance)
        finally:
            os.unlink(path)


def fresh(row):
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))).total_seconds()
        snapshot_age = (date.fromisoformat(_day()) - date.fromisoformat(row["source_date"])).days
        return (0 <= age <= MAX_AGE_SECONDS and 0 <= snapshot_age <= 2 and
                urlparse(row["source_url"]).hostname in SOURCE_HOSTS and
                bool(re.fullmatch(r"[a-f0-9]{64}", row.get("sha256", ""))))
    except (KeyError, TypeError, ValueError):
        return False


def _in_period(row, on):
    return ((not row.get("valid_from") or row["valid_from"] <= on) and
            (not row.get("valid_to") or on <= row["valid_to"]))


def eligible(row, role=None, on=None, mode=None):
    day = _day(on)
    if not row or row.get("state") != "valid" or not row.get("valid_from") or not _in_period(row, day):
        return False
    if role is None:
        return True
    traffic = MODES.get(mode, mode)
    return any(c["role"] == role.upper() and _in_period(c, day) and
               (traffic is None or c.get("traffic_type") == traffic) for c in row.get("competence", []))


async def office(code):
    return await db.raw.customs_offices.find_one({"reference_number": str(code or "").strip().upper()}, {"_id": 0})


async def offices_for(country, role, unlocode=None, mode=None):
    query = {}
    if country:
        query["country"] = country.upper()
    if role:
        query["roles"] = role.upper()
    if unlocode:
        query["unlocode"] = unlocode.replace(" ", "").upper()
    rows = await db.raw.customs_offices.find(query, {"_id": 0}).sort("reference_number", 1).to_list(None)
    return [r for r in rows if eligible(r, role, mode=mode)]


async def valid(code, role, on):
    """Role/date validity in the loaded snapshot. verify() additionally enforces freshness."""
    return eligible(await office(code), role, on)


async def verify(reference, role=None, on=None):
    row = await office(reference)
    if not row or not fresh(row):
        raise OfficeDataUnavailable(f"EU COL -tieto puuttuu tai on vanhentunut / Missing or stale EU COL evidence: {reference}")
    if not eligible(row, role, on):
        raise ValueError(f"Tullitoimipaikka ei kelpaa / Office {reference} is not valid for {role or 'use'} on {_day(on)}")
    return row


async def nearest(role, lat, lon, country=None):
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid coordinates")
    rows = [r for r in await offices_for(country, role) if r.get("lat") is not None and r.get("lon") is not None]
    if not rows:
        raise OfficeDataUnavailable("COL ei sisällä koordinaatteja / No official COL coordinates available for this selection")
    def distance(r):
        a, b = math.radians(lat), math.radians(r["lat"])
        h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(r["lon"]-lon)/2)**2
        return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))
    best = min(rows, key=distance)
    return {**best, "distance_km": round(distance(best), 3)}


async def presentation(unlocode, mode):
    """Arrival presentation uses ENT, not PCI (centralised import clearance).

    Several offices can serve one UN/LOCODE: return all candidates for review,
    never choose the first row or infer a cargo/passenger competence from a name.
    """
    sample = await db.raw.customs_offices.find_one({}, {"_id": 0})
    if not sample or not fresh(sample):
        raise OfficeDataUnavailable("COL snapshot unavailable or stale")
    if not unlocode:
        return []
    return [r for r in await offices_for(unlocode[:2], "ENT", unlocode, mode) if fresh(r)]


def _code(value):
    return value.get("code") or value.get("referenceNumber") if isinstance(value, dict) else value


async def validation_reasons(decl, kind, *, on=None):
    """COL business rules, separate from message shape/XSD checks. No network I/O.

    ENS house filings may contain no first-entry office by design; validate every
    supplied first-entry office, and require it for complete road/rail filings.
    """
    checks = []
    if kind == "export":
        checks = [("office", _code(decl.get("office")), "EXP"),
                  ("exit_office", _code(decl.get("exit_office")), "EXT")]
    elif kind == "transit":
        checks = [("office_of_departure", decl.get("office_of_departure"), "DEP"),
                  ("office_of_destination", decl.get("office_of_destination"), "DES")]
        checks += [("offices_of_transit", _code(o), "TRA") for o in decl.get("offices_of_transit", [])]
    elif kind == "ens":
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "customsOfficeOfFirstEntry":
                        checks.append((key, _code(item), "ENT"))
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(decl.get("ens_data") or {})
        if not checks and decl.get("mode") in {"road", "rail"}:
            checks.append(("customsOfficeOfFirstEntry", None, "ENT"))
    else:
        raise ValueError("Unsupported office validation kind")
    reasons = []
    for field, code, role in checks:
        try:
            await verify(code, role, on)
        except ValueError as exc:
            reasons.append({"code": "COL-" + role, "field": field,
                            "fi": f"Tullitoimipaikan {code or '(puuttuu)'} rooli {role} tai COL-voimassaolo ei kelpaa.",
                            "en": str(exc)})
    return reasons


async def enforce(decl, kind, *, on=None):
    reasons = await validation_reasons(decl, kind, on=on)
    if reasons:
        raise ValueError("; ".join(r["fi"] + " / " + r["en"] for r in reasons))


async def verify_transit_xml(xml):
    """Validate actual national/EU office elements, including nested transit offices.

    Top-level declaration checks alone miss ncts_data/national_data overrides.
    """
    from services.customs_messages.schema_codec import parse_xml
    roles = {"DepartureOffice": "DEP", "DestinationOffice": "DES", "TransitOffice": "TRA",
             "ExitOffice": "TXT", "TransitOfficeActual": "TRA", "ExitOfficeActual": "TXT",
             "CustomsOfficeOfDeparture": "DEP", "CustomsOfficeOfDestinationDeclared": "DES",
             "CustomsOfficeOfDestinationActual": "DES", "CustomsOfficeOfTransitDeclared": "TRA",
             "CustomsOfficeOfExitForTransitDeclared": "TXT"}
    for node in parse_xml(xml).iter():
        role = roles.get(etree.QName(node).localname)
        if role:
            code = next((e.text for e in node if etree.QName(e).localname in {"Identification", "referenceNumber"}), None)
            await verify(code, role)


@job("customs.offices_refresh", max_attempts=4, backoff=(60, 300, 1800, 3600), timeout=300,
     label_fi="Tullitoimipaikkaluettelon päivitys", label_en="Refresh EU customs offices")
async def offices_refresh_job(j):
    return await refresh()


schedule("customs.offices_refresh", job="customs.offices_refresh", every_seconds=86400)()
