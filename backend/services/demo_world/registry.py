"""Registry protocol answers for registered world identities; unrelated parties use the real transports."""
import re
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
from lxml import etree as E

from core.db import db
from core.util import now_iso


async def _trace(row, register):
    from services.demo_autoquote.events import emit
    if await db.raw.demo_runs.find_one({"id": row["run_id"]}, {"_id": 1}):
        await emit(row["run_id"], "booking", f"{register} registry replied", kind="world", simulated=True,
                   edge=("w:registry", "booking"), data={"facts": {"registry": register, "business_id": row["business_id"]}})


async def http_response(method, url, *, content=None):
    parsed = urlparse(url)
    query = None
    register = None
    if method.upper() == "GET" and parsed.hostname == "avoindata.prh.fi" and parsed.path == "/opendata-ytj-api/v3/companies":
        bid = (parse_qs(parsed.query).get("businessId") or [None])[0]
        query, register = {"business_id": bid}, "YTJ"
    elif method.upper() == "GET" and parsed.hostname == "ec.europa.eu" and "/vies/rest-api/ms/" in parsed.path:
        match = re.fullmatch(r"/taxation_customs/vies/rest-api/ms/([A-Z]{2})/vat/([0-9A-Z]+)", parsed.path)
        if match:
            query, register = {"vat_id": match[1] + match[2]}, "VIES"
    elif method.upper() == "GET" and parsed.hostname == "api.gleif.org" and parsed.path == "/api/v1/lei-records":
        bid = (parse_qs(parsed.query).get("filter[entity.registeredAs]") or [None])[0]
        if bid:
            query, register = {"business_id": bid}, "GLEIF"
    elif method.upper() == "POST" and parsed.hostname == "ec.europa.eu" and parsed.path == "/taxation_customs/dds2/eos/validation/services/validation" and content:
        root = E.fromstring(content, E.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
        numbers = root.xpath("//*[local-name()='eori']/text()")
        query, register = {"eori": {"$in": numbers}}, "EOS EORI"
    if query is None:
        return None
    row = await db.raw.world_registry.find_one(query, {"_id": 0}, sort=[("created_at", -1)])
    if not row:
        return None
    identity, at = row["identity"], now_iso()
    addr = identity["address"]
    req = httpx.Request(method, url)
    await _trace(row, register)
    if register == "GLEIF":
        # The private requester has no LEI or corporate parent. Do not invent registry identifiers.
        return httpx.Response(200, request=req, json={"data": []})
    if register == "YTJ":
        return httpx.Response(200, request=req, json={"companies": [{"businessId": {"value": row["business_id"]},
            "names": [{"name": identity["company"], "type": "1"}], "status": "2", "tradeRegisterStatus": "1",
            "registrationDate": row["created_at"][:10], "companyForms": [{"descriptions": [{"languageCode": "1", "description": "Osakeyhtiö"}]}],
            "addresses": [{"type": 1, "street": addr["street"], "postCode": addr["postal_code"],
                           "postOffices": [{"city": addr["city"], "languageCode": "1"}]}],
            "registeredEntries": [{"register": "6", "type": "80"}, {"register": "5", "type": "1"}], "companySituations": []}]})
    if register == "VIES":
        return httpx.Response(200, request=req, json={"isValid": True, "userError": "VALID", "name": identity["company"],
            "address": f"{addr['street']}, {addr['postal_code']} {addr['city']}", "requestDate": at})
    root = E.Element("validateEORIResponse")
    result = E.SubElement(root, "result")
    for key, value in (("eori", identity["eori"]), ("status", "0"), ("statusDescr", "Valid"), ("name", identity["company"]),
                       ("street", addr["street"]), ("postalCode", addr["postal_code"]), ("city", addr["city"]), ("country", "FI")):
        E.SubElement(result, key).text = value
    return httpx.Response(200, request=req, content=E.tostring(root), headers={"content-type": "text/xml"})


async def persona(business_id):
    return await db.raw.world_registry.find_one({"business_id": business_id}, {"_id": 0}, sort=[("created_at", -1)])


async def owners_xml(business_id):
    row = await persona(business_id)
    if not row:
        return None
    await _trace(row, "PRH")
    root = E.Element("{http://schemas.xmlsoap.org/soap/envelope/}Envelope")
    body = E.SubElement(E.SubElement(root, "{http://schemas.xmlsoap.org/soap/envelope/}Body"), "body")
    owners = row["identity"].get("ubos")
    if owners is None:
        # Missing actor facts must remain an error, not a fabricated empty register.
        E.SubElement(body, "faultString").text = "Registry actor has no beneficial ownership facts"
    elif not owners:
        E.SubElement(body, "noBeneficialOwners").text = "true"
    for owner in owners or []:
        person = E.SubElement(body, "naturalPerson")
        name = (owner.get("name") or "").rsplit(" ", 1)
        E.SubElement(person, "firstname").text = name[0]
        E.SubElement(person, "surname").text = name[1] if len(name) > 1 else ""
        E.SubElement(person, "nationality").text = owner.get("country")
        E.SubElement(person, "dateOfBirth").text = owner.get("birth_date")
        control = E.SubElement(person, "beneficialOwnership")
        if owner.get("ownership_pct") is not None:
            E.SubElement(control, "ownershipExtent").text = str(owner["ownership_pct"])
    return E.tostring(root)


def _person_facts(row, name):
    people = list(row["identity"].get("ubos") or [])
    signatory = row["identity"].get("signatory") or {}
    if signatory.get("name"):
        people.append(signatory)
    norm = lambda value: " ".join(str(value or "").casefold().split())
    return [p for p in people if norm(p.get("name")) == norm(name)]


async def pep_hit(business_id, name):
    row = await persona(business_id)
    if not row or not any(p.get("pep") is True for p in _person_facts(row, name)):
        return None
    await _trace(row, "PEP")
    # A positive actor fact proves PEP status, not a particular public office or a Wikidata identity.
    source = "World registry"
    key = hashlib.sha256(f"{business_id}:{' '.join(name.casefold().split())}".encode()).hexdigest()[:32]
    return {"status": "pep", "source": source, "matches": [{"source": source, "id": key, "name": name,
        "pep": True, "rca": False, "pep_phase": "undated",
        "positions": [{"label": "PEP stated in registry actor facts", "phase": "undated"}]}],
        "sources": [{"source": source, "status": "ok", "detail": "Explicit positive PEP fact for this person"}]}


async def pep_response(method, url, *, persona_business_id, params=None, json=None):
    row = await persona(persona_business_id)
    if not row:
        return None
    parsed = urlparse(url)
    params = params or {}
    wikidata = method.upper() == "GET" and parsed.hostname == "www.wikidata.org" and parsed.path == "/w/api.php" \
        and params.get("action") == "wbsearchentities"
    opensanctions = method.upper() == "POST" and parsed.hostname == "api.opensanctions.org" and parsed.path == "/match/default"
    if not (wikidata or opensanctions):
        return None
    query = ((json or {}).get("queries") or {}).get("q") or {}
    props = query.get("properties") or {}
    name = params.get("search") if wikidata else (props.get("name") or [None])[0]
    facts = _person_facts(row, name)
    await _trace(row, "PEP")
    request = httpx.Request(method, url)
    if any(p.get("pep") is True for p in facts) or not any(p.get("pep") is False for p in facts):
        return httpx.Response(503, request=request, json={"error": "Registry actor has no public-role facts for this person"})
    payload = {"search": []} if wikidata else {"responses": {"q": {"results": []}}}
    return httpx.Response(200, request=request, json=payload)
