"""AI quoting agent: free text ("2 euro pallets 600 kg from Tallinn to Tampere next Tuesday") -> structured
quote request. The model extracts; locations are resolved against V1's location data (services.geo /
`locations`) and the rate network; ambiguous places come back as candidates for the person to choose.
When no model is configured core.llm raises LLMUnavailable and the route answers 503 (the form stays usable)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.llm import LLMUnavailable, llm_json
from services import pricing

HELSINKI = ZoneInfo("Europe/Helsinki")


class ParseFailed(Exception):
    pass


SYSTEM = """You are the quoting agent of Huolitsija, a Finnish freight forwarder.
Read the customer's description of a shipment (Finnish or English) and extract a quote request.
Never invent facts: a value that the text does not state is null and its field name goes into "missing".
Put every interpretation you made into "assumptions" (short, in the customer's language).

Return this JSON object:
{
  "origin": {"query": "<place name in its basic form: 'Lahti' for 'Lahden', 'Tampere' for 'Tampereelle'>", "unlocode": "<5-letter UN/LOCODE only if you are certain, else null>", "country": "<ISO-2 or null>"},
  "destination": {"query": "...", "unlocode": "... or null", "country": "... or null"},
  "mode": "sea" | "road" | "air" | "rail" | null,
  "mode_stated": true | false,          // true only if the text itself names the transport mode or a mode-specific unit (container, flight, truck, ferry trailer, wagon)
  "ready_date": "YYYY-MM-DD" | null,
  "incoterm": "EXW|FCA|CPT|CIP|DAP|DPU|DDP|FAS|FOB|CFR|CIF" | null,
  "incoterm_place": string | null,
  "cargo": {
    "load_type": "loose" | "FCL",
    "description": string | null,
    "packages": integer | null,
    "package_type": "pallet_eur" | "pallet_fin" | "carton" | "crate" | "other" | null,
    "weight_kg": number | null,          // TOTAL gross weight of all packages
    "volume_m3": number | null,          // TOTAL volume
    "stackable": boolean | null,
    "containers": [{"type": "20GP|40GP|40HC|45HC|20RF|40RF", "count": integer}],
    "dangerous_goods": boolean,
    "dg": {"un_number": "UN####", "class": "3", "packing_group": "II"} | null,
    "value_eur": number | null,
    "hs_hint": string | null
  },
  "customs_clearance": boolean | null,
  "customs_handling": "us" | "customer_broker" | "unknown",
  "customs_handling_evidence": "<exact quote from customer text or null>",
  "insurance": "NONE" | "ICC_A" | "ICC_B" | "ICC_C" | null,
  "missing": ["incoterm", "volume_m3", ...],
  "assumptions": ["..."],
  "summary": "<one sentence in the customer's language>",
  "summary_i18n": {"fi": "<same summary in Finnish>", "en": "<same summary in English>"},
  "confidence": 0.0-1.0
}
Rules: multiply per-unit weights by the count ("2 pallets 300 kg each" = 600 kg). Convert tonnes, lbs and cm
dimensions (L x W x H per package x count = volume). "Euro pallet", "EUR-lava", "EUR pallet" = pallet_eur
(120 x 80 cm); "FIN-lava", "teollisuuslava" = pallet_fin (120 x 100 cm). 20' = 20GP, 40' = 40GP, 40' HC / high cube = 40HC.
Resolve relative dates ("next Tuesday", "ensi tiistaina", "huomenna") against TODAY given in the prompt; a date in the
past is null. Do not guess an Incoterm, mode or value: when the text does not name the mode, mode is null and
mode_stated is false (we price every mode and the customer compares). "Dangerous goods", "VAK", "ADR", "lithium batteries" mean
dangerous_goods = true. Decide who handles customs only from the customer text, including negation in any language.
customs_handling=us only when asked to arrange customs, customer_broker only when the customer explicitly arranges
both export and import formalities through their broker, otherwise unknown. Cite an exact source quotation;
no evidence means unknown. Never infer handling from a route or Incoterm. Border necessity is computed separately
from structured territories. customs_clearance describes our requested service, never the legal requirement."""

CONTAINER_TYPES = set(pricing.CONTAINERS)


def _today_line() -> str:
    now = datetime.now(HELSINKI)
    return f"TODAY is {now.date().isoformat()} ({now.strftime('%A')}), Europe/Helsinki time."


def _clean_num(v, minimum=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > minimum else None


async def _resolve(place: dict | None, field: str, mode: str | None) -> tuple[dict | None, dict | None]:
    """-> (resolved location, unresolved entry)."""
    if not place or not (place.get("query") or place.get("unlocode")):
        return None, None
    query = (place.get("query") or "").strip()
    code = (place.get("unlocode") or "").strip().upper()
    code_loc = None
    if code and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", code):
        code_loc = await pricing.resolve_location({"unlocode": code})
        if code_loc and place.get("country") and code_loc["country"] != place["country"].upper():
            code_loc = None
    if not query:
        return (code_loc, None) if code_loc else (None, {"field": field, "query": code, "candidates": []})
    cands = await pricing.search_locations(query, limit=6, mode=mode)
    # A model-supplied UN/LOCODE is trusted only when the place search agrees (models guess codes: Lahti is FILHI,
    # not FILHT/Lehtimäki); with no search hits at all the code is the best evidence we have.
    if code_loc and any(c.get("unlocode") == code for c in cands):
        return code_loc, None
    if not cands:
        # inflected or misspelled names ("Lahden"): accept the code only if its name starts like the query,
        # otherwise offer prefix matches for the person to choose
        def fold(x: str) -> str:
            return (x or "").lower().replace("ä", "a").replace("ö", "o").replace("å", "a")[:3]

        if code_loc and fold(query) in (fold(code_loc.get("name")), fold(code_loc.get("city"))):
            return code_loc, None
        country = (place.get("country") or "").upper()
        fallback: list[dict] = []
        for n in (5, 4, 3):
            if len(query) <= n:
                continue
            found = await pricing.search_locations(query[:n], limit=8, mode=mode)
            same = [c for c in found if not country or c["country"] == country]
            if same:
                cands = same[:6]
                break
            fallback = fallback or found
        return None, {"field": field, "query": query, "candidates": cands or fallback[:6]}
    if place.get("country"):
        same = [c for c in cands if c["country"] == place["country"].upper()]
        cands = same or cands
    ql = query.lower()
    strong = [c for c in cands if c["name"].lower().startswith(ql) or (c.get("unlocode") or "").lower() == ql]
    if len(strong) >= 1 and (len(strong) == 1 or strong[0]["name"].lower() == ql or strong[0]["country"] == "FI" or
                             strong[0].get("unlocode") in pricing.net.NETWORK_LOCATIONS):
        return strong[0], None
    if cands:
        return None, {"field": field, "query": query, "candidates": cands}
    return None, {"field": field, "query": query, "candidates": []}


async def parse_request(text: str, *, locale: str = "fi", user: dict | None = None) -> dict:
    prompt = f"{_today_line()}\nCustomer language: {'Finnish' if locale == 'fi' else 'English'}.\n\nCustomer text:\n\"\"\"\n{text}\n\"\"\""
    try:
        data = await llm_json("quoting_agent", SYSTEM, prompt, max_tokens=1600,
                              ref={"type": "quote_parse"}, company_id=(user or {}).get("company_id"))
    except LLMUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - model or network failure: a person fills the form
        raise ParseFailed("The AI could not read that description right now. Fill in the form instead.") from exc
    from services.locale_backfill import bilingual_text
    try:
        bilingual_text(data, ("summary",))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ParseFailed("The AI answer was not usable. Fill in the form instead.") from exc
    if not isinstance(data, dict):
        raise ParseFailed("The AI answer was not usable. Fill in the form instead.")

    mode = data.get("mode") if data.get("mode") in ("sea", "road", "air", "rail") else None
    if mode and data.get("mode_stated") is not True:
        mode = None  # guessed by the model: price all modes instead
        mode_words = ("meri", "sea", "maantie", "road", "lento", "air", "rail", "juna", "kuljetusmuoto", "mode")
        data["assumptions"] = [a for a in (data.get("assumptions") or []) if isinstance(a, str)
                               and not any(w in a.lower() for w in mode_words)]
    missing = [m for m in (data.get("missing") or []) if isinstance(m, str)][:12]
    assumptions = [a for a in (data.get("assumptions") or []) if isinstance(a, str)][:10]
    origin, un_o = await _resolve(data.get("origin"), "origin", mode)
    dest, un_d = await _resolve(data.get("destination"), "destination", mode)
    unresolved = [u for u in (un_o, un_d) if u]
    if not origin and "origin" not in missing:
        missing.append("origin")
    if not dest and "destination" not in missing:
        missing.append("destination")

    rd = None
    if data.get("ready_date"):
        try:
            d = date.fromisoformat(str(data["ready_date"])[:10])
            if d >= datetime.now(HELSINKI).date() and d <= datetime.now(HELSINKI).date() + timedelta(days=180):
                rd = d.isoformat()
        except ValueError:
            rd = None
    if not rd and "ready_date" not in missing:
        missing.append("ready_date")

    c = data.get("cargo") or {}
    containers = []
    for row in c.get("containers") or []:
        t = str((row or {}).get("type") or "").upper().replace("'", "")
        n = int(_clean_num((row or {}).get("count"), 0) or 0)
        if t in CONTAINER_TYPES and n > 0:
            containers.append({"type": t, "count": min(n, 50)})
    cargo = {
        "load_type": "FCL" if containers else "loose",
        "description": (c.get("description") or None),
        "packages": int(_clean_num(c.get("packages")) or 0) or None,
        "package_type": c.get("package_type") if c.get("package_type") in pricing.PACKAGE_LABEL else None,
        "weight_kg": _clean_num(c.get("weight_kg")),
        "volume_m3": _clean_num(c.get("volume_m3")),
        "stackable": bool(c.get("stackable")) if c.get("stackable") is not None else False,
        "containers": containers,
        "dangerous_goods": bool(c.get("dangerous_goods")),
        "dg": c.get("dg") if c.get("dangerous_goods") and isinstance(c.get("dg"), dict) else None,
        "value_cents": int(round(_clean_num(c.get("value_eur")) * 100)) if _clean_num(c.get("value_eur")) else None,
        "hs_hint": c.get("hs_hint") or None,
    }
    if not containers and not cargo["weight_kg"] and "weight_kg" not in missing:
        missing.append("weight_kg")
    inc = (data.get("incoterm") or "").upper() or None
    if inc and inc not in pricing.INCOTERMS_2020:
        assumptions.append(f"{inc} is not an Incoterms 2020 rule" + (f"; {pricing.LEGACY_INCOTERMS[inc]} replaces it" if inc in pricing.LEGACY_INCOTERMS else ""))
        inc = None
    if not inc and "incoterm" not in missing:
        missing.append("incoterm")
    ins = data.get("insurance") if data.get("insurance") in ("NONE", "ICC_A", "ICC_B", "ICC_C") else None

    customs_handling = data.get("customs_handling")
    evidence = data.get("customs_handling_evidence")
    if customs_handling not in ("us", "customer_broker") or not isinstance(evidence, str) or not evidence.strip() or evidence not in text:
        customs_handling, evidence = "unknown", None
    request = {"mode": mode, "origin": origin, "destination": dest, "ready_date": rd, "incoterm": inc,
               "incoterm_place": data.get("incoterm_place") or None, "cargo": cargo,
               "customs_clearance": True if customs_handling == "us" else False if customs_handling == "customer_broker" else None,
               "customs_handling": customs_handling, "customs_handling_evidence": evidence,
               "insurance": {"cover": ins} if ins else None}
    filled = [k for k, v in request.items() if v not in (None, [], {})] + [f"cargo.{k}" for k, v in cargo.items() if v not in (None, [], False)]
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence") or 0)))
    except (TypeError, ValueError):
        conf = None
    return {"request": request, "missing": missing, "assumptions": assumptions, "unresolved": unresolved,
            "summary": (data.get("summary") or "")[:400], "summary_i18n": data.get("summary_i18n"), "confidence": conf, "filled": filled,
            "ai": {"agent": "quoting_agent", "text": text[:2000], "summary_i18n": data.get("summary_i18n"), "summary": (data.get("summary") or "")[:400]}}
