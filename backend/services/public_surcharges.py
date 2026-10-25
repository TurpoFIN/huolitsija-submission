"""Public provider tariffs, never carrier offers or capacity confirmations.

All money stays in the provider currency. A caller must name the provider,
service, lane and price-calculation date. No fallback to a benchmark or stale
snapshot. Read-only; no RFQ, booking, provider account or customer record.
"""
from __future__ import annotations

import asyncio
import calendar
import copy
import hashlib
import io
import json
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError

SOURCES = {
    "finnlines": "https://www.finnlines.com/wp-json/custom/v1/bunker-surcharges",
    "bluedart": "https://api.bluedart.com/currency-adjustment-factor",
    "swissport": "https://www.swissport.com/en/media/network-files/finland/thc-hel-finland-01jan26",
    "maersk": "https://www.maersk.com.cn/news/articles/2026/09/07/pss-west-mediterranean-united-states-canada",
    "arkas": "https://arkasline.com.tr/en/wp-content/uploads/2026/03/Arkas-Line-Emergency-War-Risk-Surcharge-03.03.2026.pdf",
}
# Small, reviewed scope for each provider publication; never inferred from geography.
FERRY_PAIRS = {"FINNLINK": ["FINLI", "SEKPS"], "NORDOLINK": ["SEMMA", "DETRV"],
               "POLISHLINK": ["SEMMA", "PLSWI"]}
TARIFF_CODES = {
    **{f"finnlines-baf-{x.lower()}": "BAF" for x in FERRY_PAIRS},
    "bluedart-caf":"CAF", "swissport-hel-import-thc":"DTHC", "swissport-hel-export-thc":"OTHC",
    "maersk-westmed-na-pss":"PSS", "arkas-wrs-sajed":"WAR_RISK", "arkas-wrs-djjib":"WAR_RISK",
}
WEST_MED = "AL BA CY DZ ES FR GR HR HU IT MA MT PT RS SI SK TN".split()
_CACHE: dict = {}
_LOCKS: dict = {}
TTL_SECONDS = 600


class SourceUnavailable(ValueError):
    pass


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"): self.ignore += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"): self.ignore = max(0, self.ignore - 1)

    def handle_data(self, data):
        if not self.ignore: self.parts.append(data)


def plain(body: bytes, pdf=False) -> str:
    if pdf:
        text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(body)).pages)
    else:
        parser = _Text(); parser.feed(body.decode("utf-8")); text = " ".join(parser.parts)
    return re.sub(r"\s+", " ", text).strip()


def decimal(value) -> Decimal:
    d = Decimal(str(value).replace(",", "."))
    if not d.is_finite(): raise ValueError("A finite number is required")
    return d


def money(value) -> str:
    return str(decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def iso_date(value: str) -> str:
    value = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value)
    for fmt in ("%d-%b-%y", "%d-%B-%y", "%d %B %Y", "%d-%B-%Y", "%d/%m/%Y"):
        try: return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError: pass
    return date.fromisoformat(value).isoformat()


def month_end(day: str) -> str:
    d = date.fromisoformat(day)
    return d.replace(day=calendar.monthrange(d.year, d.month)[1]).isoformat()


def _match(pattern, text):
    m = re.search(pattern, text, re.I)
    if not m: raise SourceUnavailable("Provider publication format or scope changed; review required")
    return m


def parse_source(provider: str, body: bytes) -> list[dict]:
    """Parse actual publications, including amounts, effective dates and units."""
    rows = []
    if provider == "finnlines":
        for group in json.loads(body):
            area = group["area"]
            if area not in FERRY_PAIRS: continue
            for item in group["items"]:
                if item["cargo"] != "ALL CARGO" or item["measurement"] != "EUR LM": continue
                start = iso_date(group["date"])
                rows.append(dict(id=f"finnlines-baf-{area.lower()}", code="BAF", provider=provider,
                                 label=f"Finnlines {area}", basis="PER_LDM", currency="EUR",
                                 rate=str(decimal(item["surcharge"])), effective_from=start, effective_to=month_end(start),
                                 scope={"service": "RORO", "port_pair": FERRY_PAIRS[area]},
                                 source_record_id=str(item["id"]), stage="main"))
    elif provider == "bluedart":
        text = plain(body)
        # Current declaration, rather than an unrelated historical table cell.
        m = _match(r"current surcharge, effective (\d{1,2} \w+ \d{4}), is (\d+(?:\.\d+)?)%", text)
        start = iso_date(m[1])
        rows.append(dict(id="bluedart-caf", code="CAF", provider=provider, label="Blue Dart CAF",
                         basis="PERCENT", rate=m[2], currency="INR", effective_from=start, effective_to=month_end(start),
                         scope={"service": "AIR", "origin_country": "IN", "destination_country": "IN",
                                "base": "caf_subject_amount"}, stage="main",
                         limitation="Percentage of the explicitly supplied CAF-subject amount; the publication does not define the contractual base."))
    elif provider == "swissport":
        text = plain(body, pdf=True)
        start = iso_date(_match(r"Effective from (\d{1,2}(?:st|nd|rd|th)? \w+ \d{4})", text)[1])
        for ident, code, caption, stage, field in [
            ("import", "DTHC", "Import Handling and Release Charge", "destination", "destination"),
            ("export", "OTHC", "Export Handling and Terminal Charge", "origin", "origin")]:
            m = _match(caption + r" Per Kg ([\d,.]+) ([\d,.]+) ([\d,.]+)", text)
            rows.append(dict(id=f"swissport-hel-{ident}-thc", code=code, provider=provider,
                             label=f"Swissport HEL {ident} THC", basis="PER_KG", currency="EUR",
                             rate=str(decimal(m[1])), minimum=str(decimal(m[2])), maximum=str(decimal(m[3])),
                             effective_from=start, effective_to=None, scope={"service": "AIR", field: "FIHEL"},
                             stage=stage, limitation="Handling charge only; storage, security, special handling and applicable taxes excluded."))
    elif provider == "maersk":
        text = plain(body)
        m = _match(r"West Mediterranean to United States and Canada with effective from ([\dA-Za-z -]+?) until further notice", text)
        start = iso_date(m[1])
        # Fail closed if the reviewed origin list changes or price columns diverge.
        _match(r"Albania, Bosnia and Herzegovina, Cyprus, Algeria, Spain, France, Greece, Croatia, Hungary, Italy, Morocco, Malta, Portugal, Serbia, Slovenia, Slovakia and Tunisia", text)
        p = _match(r"West Mediterranean United States & Canada Per container USD (\d+) (\d+) (\d+)", text)
        rows.append(dict(id="maersk-westmed-na-pss", code="PSS", provider=provider, label="Maersk West Mediterranean → US/Canada",
                         basis="PER_CONTAINER", currency="USD", rates={"20GP":p[1], "20RF":p[1], "40GP":p[2], "40HC":p[2], "45HC":p[2], "40RF":p[3]},
                         effective_from=start, effective_to=None, scope={"service":"FCL", "origin_countries":WEST_MED,
                         "destination_countries":["US","CA"], "date_basis":"carrier_price_calculation_date"}, stage="main",
                         limitation="Use the carrier PCD: last gate-in for FMC non-SPOT; first vessel ETD for SPOT. Not the cargo-ready date."))
    elif provider == "arkas":
        text = plain(body, pdf=True)
        start = iso_date(_match(r"\b(\d{2}/\d{2}/\d{4})\b", text)[1])
        _match(r"For special equipment separate announcement", text)
        _match(r"For USA shipments, a separate announcement", text)
        amounts = re.findall(r"USD ([\d,]+) per 20[’'] Dry Container \| USD ([\d,]+) per 40[’'] Dry/HC Container", text)
        if len(amounts) != 2: raise SourceUnavailable("Arkas tariff rows changed")
        for (a,b), ports in zip(amounts, [["SAJED","JOAQJ"],["DJJIB","SDPZU"]]):
            if not all(p in text for p in ports): raise SourceUnavailable("Arkas port scope changed")
            rows.append(dict(id=f"arkas-wrs-{ports[0].lower()}", code="WAR_RISK", provider=provider,
                             label=f"Arkas EWRS {' / '.join(ports)}", basis="PER_CONTAINER", currency="USD",
                             rates={"20GP":a.replace(",",""),"40GP":b.replace(",",""),"40HC":b.replace(",","")},
                             effective_from=start, effective_to=None, scope={"service":"FCL", "either_ports":ports,
                             "excluded_countries":["US"], "date_basis":"booking_date"}, stage="main",
                             limitation="Dry equipment only; US trade and special equipment need a separate carrier notice."))
    if not rows: raise SourceUnavailable("No supported tariff rows in provider publication")
    return rows


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, max_redirects=3,
                                 headers={"User-Agent":"Huolitsija/1.0 public-tariff-reader"}) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            data = bytearray()
            async for chunk in r.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000: raise SourceUnavailable("Provider response exceeds tariff size limit")
            return bytes(data)


async def provider_rows(provider: str) -> dict:
    if provider not in SOURCES: raise ValueError("Unknown tariff provider")
    lock = _LOCKS.setdefault(provider, asyncio.Lock())
    async with lock:
        cached = _CACHE.get(provider)
        if cached and time.monotonic() - cached[0] < TTL_SECONDS: return copy.deepcopy(cached[1])
        try:
            body = await asyncio.wait_for(_download(SOURCES[provider]), timeout=20)
            rows = await asyncio.to_thread(parse_source, provider, body)
            stamp = datetime.now(timezone.utc).isoformat()
            source = {"type":"public_provider_tariff", "provider":provider, "url":SOURCES[provider],
                      "retrieved_at":stamp, "sha256":hashlib.sha256(body).hexdigest()}
            for r in rows:
                r.update(source=source, bookable=False, tax_included=False)
            result = {"provider":provider, "state":"available", "items":rows, "source":source}
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, PyPdfError, ArithmeticError) as exc:
            result = {"provider":provider, "state":"unavailable", "items":[],
                      "source_url":SOURCES[provider], "reason":f"Provider tariff could not be verified ({type(exc).__name__})"}
        _CACHE[provider] = (time.monotonic(), result)
        return copy.deepcopy(result)


async def catalog() -> dict:
    sources = await asyncio.gather(*(provider_rows(p) for p in SOURCES))
    # Current and announced rows only; historical monthly rows remain selectable by date in calculate.
    today = date.today().isoformat()
    items = [r for s in sources for r in s["items"] if not r.get("effective_to") or r["effective_to"] >= today]
    return {"items":items, "sources":[{k:v for k,v in s.items() if k != "items"} for s in sources],
            "scope":"Public surcharges only. Freight, capacity and provider acceptance are separate.", "bookable":False}


def evaluate(row: dict, ctx: dict) -> dict:
    """One independently calculated charge; never a combined freight quote."""
    out = {"tariff":row, "state":"not_applicable", "amount":None, "currency":row["currency"], "bookable":False}
    scope = row["scope"]
    if ctx.get("provider") != row["provider"] or ctx.get("service") != scope["service"]:
        return {**out, "reason":"provider_or_service_mismatch"}
    origin, dest = ctx.get("origin", ""), ctx.get("destination", "")
    if not origin or not dest: return {**out,"state":"needs_input","reason":"origin_and_destination_required"}
    cc = {origin[:2],dest[:2]}
    if scope.get("port_pair") and {origin,dest} != set(scope["port_pair"]): return {**out,"reason":"lane_mismatch"}
    if scope.get("either_ports") and not {origin,dest}.intersection(scope["either_ports"]): return {**out,"reason":"lane_mismatch"}
    for side, value in (("origin",origin),("destination",dest)):
        if scope.get(side) and value != scope[side]: return {**out,"reason":"terminal_mismatch"}
        if scope.get(side+"_country") and value[:2] != scope[side+"_country"]: return {**out,"reason":"country_mismatch"}
        if scope.get(side+"_countries") and value[:2] not in scope[side+"_countries"]: return {**out,"reason":"country_mismatch"}
    if cc.intersection(scope.get("excluded_countries",[])): return {**out,"reason":"excluded_trade"}
    day = ctx.get("on_date")
    if not day: return {**out,"state":"needs_input","reason":"price_calculation_date_required"}
    if scope.get("date_basis") and ctx.get("date_basis") != scope["date_basis"]:
        return {**out,"state":"needs_input","reason":scope["date_basis"]+"_required"}
    if day < row["effective_from"]:
        return {**out,"reason":"not_yet_effective"}
    if row.get("effective_to") and day > row["effective_to"]:
        return {**out,"state":"unavailable","reason":"tariff_period_expired"}
    basis = row["basis"]
    qty_name = {"PER_LDM":"lane_metres", "PER_KG":"chargeable_kg", "PER_CONTAINER":"containers", "PERCENT":"caf_subject_amount"}[basis]
    qty = ctx.get(qty_name)
    if qty is None: return {**out,"state":"needs_input","reason":qty_name+"_required"}
    qty = decimal(qty)
    if qty <= 0: return {**out,"state":"needs_input","reason":qty_name+"_must_be_positive"}
    if basis == "PER_CONTAINER":
        rate = row["rates"].get(ctx.get("equipment"))
        if rate is None: return {**out,"reason":"equipment_not_published"}
        if qty != qty.to_integral_value(): return {**out,"state":"needs_input","reason":"whole_container_count_required"}
    else: rate = row["rate"]
    if basis == "PERCENT" and ctx.get("base_currency") != row["currency"]:
        return {**out,"state":"needs_input","reason":"CAF_base_currency_must_be_"+row["currency"]}
    amount = qty * decimal(rate) / (100 if basis == "PERCENT" else 1)
    if "minimum" in row: amount = max(amount,decimal(row["minimum"]))
    if "maximum" in row: amount = min(amount,decimal(row["maximum"]))
    return {**out,"state":"calculated","amount":money(amount),"quantity":str(qty),"rate":str(rate),
            "tax_included":False,"reason":"public_tariff_calculation"}


async def calculate(tariff_id: str, ctx: dict) -> dict:
    provider = tariff_id.split("-",1)[0]
    result = await provider_rows(provider)
    if result["state"] != "available": return {**result,"amount":None,"bookable":False}
    matches = [r for r in result["items"] if r["id"] == tariff_id]
    if not matches: raise ValueError("Unknown tariff")
    day = ctx.get("on_date") or date.today().isoformat()
    past = [r for r in matches if r["effective_from"] <= day]
    row = max(past or matches,key=lambda r:r["effective_from"])
    return evaluate(row,ctx)


async def quote_lines(req: dict, opt: dict, card: dict, carrier: dict, metrics: dict, existing_lines: list) -> tuple[list, list]:
    """Explicit rate-card opt-in. No ref means no IO and no changed price.

    References never replace an inclusive or already itemized charge. A missing
    required tariff/PCD/FX makes the option incomplete; the pricing owner removes
    it from the offer set. Ops supplies the carrier's contractual applicability,
    not a fabricated buy rate. Original tariff and FX provenance stay on lines.
    """
    refs = card.get("public_surcharge_refs") or []
    if not refs: return [], []
    from services import pricing, fx
    lines, errors = [], []
    main = next((leg for leg in opt.get("legs", []) if leg.get("kind") == "main"), None)
    if not main: return [], ["main_leg_required"]
    provider_codes = {"finnlines":"FINNLINES", "maersk":"MAEU", "arkas":"ARKAS", "bluedart":"BLUEDART"}
    seen = set()
    for ref in refs:
        tariff_id = ref.get("tariff_id", "")
        if tariff_id in seen:
            errors.append(tariff_id+":duplicate_reference"); continue
        seen.add(tariff_id)
        provider = tariff_id.split("-",1)[0]
        if provider not in SOURCES:
            errors.append("unknown_provider"); continue
        code = TARIFF_CODES.get(tariff_id)
        if not code:
            errors.append(tariff_id+":invalid_tariff"); continue
        aliases = {"WAR_RISK", "WRS"} if code == "WAR_RISK" else {code}
        if set(card.get("included_charge_codes") or []).intersection(aliases) or any(
                l.get("code") in aliases and l.get("leg_seq") == main["seq"] for l in existing_lines):
            continue  # carrier inclusion is authoritative even if the public feed is unavailable
        # Swissport is a separately named terminal operator, not an airline.
        if provider == "swissport":
            side = "origin" if "-export-" in tariff_id else "destination"
            bound = card.get(side+"_terminal_operator") == provider
        else:
            bound = carrier.get("code", "").upper() == provider_codes[provider]
        if not bound:
            errors.append(tariff_id+":provider_binding_required"); continue
        ctx = {"provider":provider, "service":card.get("service"),
               "origin":(main.get("from") or {}).get("unlocode"),
               "destination":(main.get("to") or {}).get("unlocode"),
               "on_date":ref.get("on_date") or req["ready_date"],
               "date_basis":ref.get("date_basis") or "service_date",
               "lane_metres":pricing.chargeable(card["service"],metrics).get("vehicles",1)*pricing.TRAILER_LDM,
               "chargeable_kg":pricing.chargeable("AIR",metrics)["qty"],
               "caf_subject_amount":ref.get("caf_subject_amount"), "base_currency":ref.get("base_currency")}
        # PCD/booking date must come from the provider terms, never silently use cargo-ready date.
        if provider in ("maersk", "arkas") and not ref.get("on_date"):
            errors.append(tariff_id+":provider_price_date_required"); continue
        batches = metrics.get("containers") if card.get("service") == "FCL" else [None]
        for container in batches or [None]:
            if container: ctx.update(equipment=container["type"],containers=container["count"])
            try: result = await calculate(tariff_id,ctx)
            except ValueError:
                errors.append(tariff_id+":invalid_tariff"); continue
            if result.get("reason") == "not_yet_effective": continue
            if result.get("state") != "calculated":
                errors.append(tariff_id+":"+result.get("reason", "source_unavailable")); continue
            row = result["tariff"]
            code = row["code"]
            aliases = {"WAR_RISK","WRS"} if code == "WAR_RISK" else {code}
            included = set(card.get("included_charge_codes") or [])
            if included.intersection(aliases) or any(l.get("code") in aliases and l.get("leg_seq") == main["seq"] for l in existing_lines):
                continue  # explicit carrier inclusion/itemization has precedence
            foreign_cents = int(decimal(result["amount"])*100)
            if row["currency"] == "EUR": eur_cents, exchange = foreign_cents, None
            else:
                try:
                    eur_cents, exchange = await asyncio.wait_for(fx.to_eur(foreign_cents,row["currency"], date.today()),timeout=20)
                    # fx.eur_rate may return an older stored rate after a failed refresh; reject it here.
                    if (date.today()-date.fromisoformat(exchange["rate_date"])).days > 4:
                        raise fx.FxUnavailable("Stale FX")
                except (fx.FxUnavailable, TimeoutError):
                    errors.append(tariff_id+":current_FX_required"); continue
            ln = pricing._cost_line(code,row["stage"],"PER_SHIPMENT",1,eur_cents,leg_seq=main["seq"],note=row["label"])
            ln.update(source=row["source"], public_tariff_id=tariff_id, public_tariff_calculation=result,
                      original_amount=result["amount"], original_currency=row["currency"], fx=exchange)
            lines.append(ln)
    return lines, errors
