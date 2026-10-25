"""The fictional requester and registry agree on one durable identity per run."""
import hashlib
import json
import random
from urllib.parse import urlencode

import httpx

from core.db import db
from core.util import now_iso
from . import llm


def _business_id(seed):
    rng = random.Random(f"{seed}:w:registry:0")
    for _ in range(100):
        digits = "399" + str(rng.randrange(10000)).zfill(4)
        remainder = sum(int(x) * w for x, w in zip(digits, (7, 9, 10, 5, 8, 4, 2))) % 11
        if remainder != 1:
            return digits + "-" + str(0 if remainder == 0 else 11 - remainder)
    raise RuntimeError("Cannot allocate fictional registry identity")


async def requester_identity(run: dict) -> dict:
    """Called by lane 1 before intake. Returns facts, never a KYC approval.

    The reserved identifier must be absent from a bounded real YTJ lookup before
    it is entered into the world. Real company identities are never reused.
    """
    run_id = run["id"]
    row = await db.raw.world_registry.find_one({"run_id": run_id}, {"_id": 0})
    if row:
        return row["identity"]
    from .boundary import requester_address
    mailbox = requester_address()
    previous = await db.raw.world_registry.find_one({"requester_address": mailbox,
        "verified_name_absent": {"$exists": True}}, {"_id": 0}) if mailbox else None
    if previous:
        await db.raw.world_registry.update_one({"_id": run_id}, {"$setOnInsert": {**previous, "run_id": run_id}}, upsert=True)
        return previous["identity"]
    for attempt in range(3):
        bid = _business_id(run["seed"] if attempt == 0 else f"{run['seed']}:{attempt}")
        url = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId=" + bid
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            response = await client.get(url)
        if response.status_code not in (200, 404):
            raise RuntimeError("Real YTJ did not answer the fictional-identity collision check")
        absent = response.status_code == 404 or not (response.json().get("companies") or [])
        if absent:
            break
    else:
        raise RuntimeError("Fictional requester identity collides with registered businesses")
    profile = run.get("profile") or {}
    rejected_names = []
    for name_attempt in range(4):
        persona = await llm.ask_json("w:registry", "Create one fictional private Finnish shipper persona for a logistics world. "
        "Never use a real company or person's identity. Return {company, contact_name, address: {street,postal_code,city,country}, "
        "signatory: {name,title,pep:false}, ubos: [{name,country,ownership_pct,pep:false}]}. State the PEP fact explicitly "
        "for every signatory and owner. Use a plausible Finnish company name "
        "combining an invented place or surname and the relevant industry, ending in Oy. Never include Demo, Fictional, "
        "a seed, a token, a hash or a number in the company name. Use a natural invented two-part Finnish contact name, "
        "postal address and one 100% private owner, no public role. Country FI. Do not create goods or commercial facts.",
        json.dumps({"seed_token": hashlib.sha256(str(run["seed"]).encode()).hexdigest()[:6], "attempt": name_attempt,
                    "unavailable_names": rejected_names, "origin": run.get("origin_text"),
                    "destination": run.get("destination_text"), "profile": profile}, default=str), run_id=run_id)
        name = str(persona.get("company") or "").strip()
        if not name.endswith(" Oy") or any(c.isdigit() for c in name) or any(w in name.lower() for w in ("demo", "fictional")):
            rejected_names.append(name)
            continue
        name_url = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies?" + urlencode({"name": name})
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            name_response = await client.get(name_url)
        if name_response.status_code not in (200, 404):
            raise RuntimeError("Real YTJ did not answer the fictional-name collision check")
        if name_response.status_code == 404 or not (name_response.json().get("companies") or []):
            break
        rejected_names.append(name)
    else:
        raise RuntimeError("Cannot allocate an unregistered plausible Finnish company name")
    # Number, country and exact signatory identity are protocol invariants, not LLM choices.
    required = (persona.get("company"), persona.get("contact_name"), persona.get("address"), persona.get("ubos"))
    if not all(required) or len(str(persona["contact_name"]).split()) < 2:
        raise ValueError("Registry persona omitted its fictional identity facts")
    address = persona["address"]
    if not all(address.get(k) for k in ("street", "postal_code", "city")):
        raise ValueError("Registry persona omitted registered address")
    signatory = persona.get("signatory") or {}
    if not isinstance(signatory.get("pep"), bool) or any(not isinstance(u.get("pep"), bool) for u in persona["ubos"]):
        raise ValueError("Registry persona omitted an explicit PEP fact for a signatory or owner")
    address["country"] = "FI"
    identity = {"company": persona["company"], "business_id": bid, "vat_id": "FI" + bid.replace("-", ""),
                "eori": "FI" + bid, "address": address, "contact_name": persona["contact_name"],
                "signatory": {"name": persona["contact_name"], "title": signatory.get("title") or "Managing director", "pep": signatory["pep"]},
                "ubos": persona["ubos"]}
    await db.raw.world_registry.insert_one({"_id": run_id, "run_id": run_id, "requester_address": mailbox, "business_id": bid, "vat_id": identity["vat_id"],
        "eori": identity["eori"], "identity": identity, "verified_absent": {"url": url, "http_status": response.status_code,
        "body_sha256": hashlib.sha256(response.content).hexdigest(), "at": now_iso()},
        "verified_name_absent": {"url": name_url, "http_status": name_response.status_code,
        "body_sha256": hashlib.sha256(name_response.content).hexdigest(), "at": now_iso()}, "created_at": now_iso()})
    return identity


async def document_parties(run: dict, quote: dict) -> dict:
    """The world's durable trading parties, supplied to mail and source files alike."""
    parties = {}
    request = quote.get('request') or quote
    for role, place in (("shipper", request.get("origin") or {}), ("consignee", request.get("destination") or {})):
        key = run["id"] + ":" + role
        row = await db.raw.world_party_identities.find_one({"_id": key})
        if row:
            parties[role] = row["identity"]
            continue
        if place.get("country") == "FI" and run.get("requester_registry"):
            source = run["requester_registry"]
            party = {"name": source["company"], **{k: source[k] for k in ("address", "contact_name", "business_id", "vat_id", "eori")}}
            city = place.get('city') or place.get('name')
            if city and city.casefold() != source['address']['city'].casefold():
                operating = await llm.ask_json('w:customer', 'Supply the full postal address of your fictional company operating site in the supplied city. '
                    'This is a fictional customer-owned pickup/delivery warehouse, distinct from its registered office. '
                    'Return {street,postal_code,city,country}. Preserve the supplied city/country and any supplied postcode. '
                    'Never borrow a real third-party company identity.', json.dumps({'company':source['company'], 'place':place,
                        'postcode':(run.get('profile') or {}).get('pickup_postcode' if role == 'shipper' else 'delivery_postcode'),
                        'seed':run['seed']}), run_id=run['id'])
                if not all(operating.get(k) for k in ('street','postal_code','city','country')) or operating['country'] != 'FI' \
                        or operating['city'].casefold() != city.casefold():
                    raise ValueError('World operating site needs a full Finnish address')
                party.update(address=operating, registered_address=source['address'])
            receipt = {"source": "requester_registry", "business_id": source["business_id"]}
        else:
            unavailable = []
            for attempt in range(4):
                party = await llm.ask_json("w:" + role, "Create a fictional trading company and natural fictional contact for the supplied location. "
                    "Return {name,contact_name,address:{street,postal_code,city,country}}. Use a plausible local business name appropriate "
                    "for the goods and country's legal suffix. Never use a known real company or person. Never use Demo, Fictional, "
                    "a numeric token or a hash in a name. Do not invent tax or registry numbers. Preserve the supplied city and country.",
                    json.dumps({"role": role, "place": place, "profile": run.get("profile"), "seed": run["seed"],
                        "attempt": attempt, "unavailable_names": unavailable}, default=str), run_id=run["id"])
                name = str(party.get("name") or "").strip()
                address = party.get("address") or {}
                if not name or any(w in name.lower() for w in ("demo", "fictional")) or not party.get("contact_name") \
                        or not all(address.get(k) for k in ("street", "postal_code", "city", "country")) \
                        or address["country"] != place.get("country"):
                    unavailable.append(name)
                    continue
                url = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies?" + urlencode({"name": name})
                async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                    response = await client.get(url)
                if response.status_code not in (200, 404):
                    raise RuntimeError("YTJ did not answer the world-party name collision check")
                if response.status_code == 404 or not (response.json().get("companies") or []):
                    receipt = {"url": url, "http_status": response.status_code,
                        "body_sha256": hashlib.sha256(response.content).hexdigest(), "at": now_iso()}
                    break
                unavailable.append(name)
            else:
                raise RuntimeError("Cannot allocate an unregistered world trading-party name")
        await db.raw.world_party_identities.update_one({"_id": key}, {"$setOnInsert": {
            "run_id": run["id"], "role": role, "identity": party, "verified_name_absent": receipt, "created_at": now_iso()}}, upsert=True)
        parties[role] = party
    return parties
