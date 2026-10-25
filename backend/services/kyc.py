"""Customer due diligence (KYC) against live public registries.

Checks (each stored in companies.kyc.<check> with status pass|warn|fail|error|not_applicable, checked_at,
source, url, summary, data):
  ytunnus   Business ID check digit (RULE E-2)
  ytj       PRH / Vero YTJ open data API v3: registered, Business ID valid, no liquidation/bankruptcy/restructuring,
            VAT register entry (https://avoindata.prh.fi/opendata-ytj-api/v3/companies?businessId=)
  vies      EU VIES REST API (https://ec.europa.eu/taxation_customs/vies/rest-api/ms/FI/vat/<8 digits>) (RULE V-2)
  eori      EU EOS EORI validation SOAP service, operation validateEORI (RULE E-1). Finnish EORIs are held in EOS as
            FI + Business ID with the hyphen (verified live: FI0109862-8 valid, FI01098628 not valid).
  sanctions company name, auxiliary names, UBOs and signatory against the EU, UN and OFAC lists (services/screening)
  ubo       beneficial owners (> 25 %) declared, compared with registry sources (services/beneficial_owners: GLEIF
            parents, PRH beneficial owner register when configured, UK PSC register)
  pep       every declared owner, registry owner and the signatory against PEP sources (services/pep: Wikidata, or
            OpenSanctions when keyed). A PEP or family-member hit is status `review` and legal point `pep_relationship`:
            only a person with kyc.approve can approve the relationship (never an agent).
EORI is a customs-readiness check: a missing EORI does not fail KYC but blocks customs filing (RULE E-1).
Decision: every risk check passes and settings.auto_approve_low_risk_kyc -> approved; otherwise in_review plus
a "kyc_review" approval for a person with kyc.approve. Records are kept >= 5 years (AML practice).
"""
from __future__ import annotations

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET

import httpx
from fastapi import HTTPException

from core import recorded_http
from core.approvals import cancel_approvals, request_approval
from core.audit import actor_ref, audit
from core.db import db
from core.events import emit, on
from core.notify import bi, notify_company, notify_role, same
from core.refs import ytunnus_valid
from core.settings import get_settings
from core.util import NO_ID, now_iso

log = logging.getLogger("kyc")

YTJ_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"
VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{ms}/vat/{num}"
EOS_URL = "https://ec.europa.eu/taxation_customs/dds2/eos/validation/services/validation"
SITUATIONS = {"SANE": "restructuring", "SELTILA": "liquidation", "KONK": "bankruptcy"}
RISK_CHECKS = ("ytunnus", "ytj", "vies", "sanctions", "ubo", "pep")
# Check names in the Finnish KYC review notification.
CHECK_FI = {"ytunnus": "Y-tunnus", "ytj": "YTJ", "vies": "ALV-tunniste (VIES)", "sanctions": "Pakotelistat",
            "ubo": "Tosiasialliset edunsaajat", "pep": "Poliittisesti vaikutusvaltaiset henkilöt", "eori": "EORI", "poa": "Valtakirja"}
UA = {"User-Agent": "Huolitsija KYC/1.0 (+https://huolitsija.fi)"}


def normalise_business_id(v: str | None) -> str | None:
    if not v:
        return None
    # NFKC folds full-width digits and other compatibility forms to ASCII; dashes of any kind become "-".
    s = unicodedata.normalize("NFKC", str(v))
    s = "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() and not ch.isascii() else ch for ch in s)
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", s)
    s = re.sub(r"\s", "", s).upper()
    s = s[2:] if s.startswith("FI") else s
    m = re.fullmatch(r"([0-9]{6,7})-?([0-9])", s)
    if not m:
        return None
    return f"{m.group(1).rjust(7, '0')}-{m.group(2)}"


def _desc(descs: list | None, lang: str = "3") -> str | None:
    for d in descs or []:
        if d.get("languageCode") == lang:
            return d.get("description")
    return (descs or [{}])[0].get("description") if descs else None


# ---------------------------------------------------------------------------------------------
# YTJ
# ---------------------------------------------------------------------------------------------
async def ytj_lookup(business_id: str) -> dict:
    bid = normalise_business_id(business_id)
    if not bid or not ytunnus_valid(bid):
        raise HTTPException(400, "Business ID must look like 1234567-8 and have a valid check digit")
    url = f"{YTJ_URL}?businessId={bid}"
    try:
        r = await recorded_http.request("GET", url, headers=UA, timeout=20)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"The YTJ business register did not answer ({type(exc).__name__}); try again") from exc
    if r.status_code == 404:
        return {"found": False, "business_id": bid, "source": "YTJ", "url": url, "fetched_at": now_iso()}
    if r.status_code != 200:
        raise HTTPException(502, f"The YTJ business register answered HTTP {r.status_code}; try again")
    data = r.json()
    comps = data.get("companies") or []
    if not comps:
        return {"found": False, "business_id": bid, "source": "YTJ", "url": url, "fetched_at": now_iso()}
    c = comps[0]
    names = c.get("names") or []
    main = next((n for n in names if n.get("type") == "1" and not n.get("endDate")), names[0] if names else {})
    aux = [n.get("name") for n in names if n.get("type") != "1" and not n.get("endDate") and n.get("name")]
    addrs = c.get("addresses") or []
    addr = next((a for a in addrs if a.get("type") == 1), None) or next((a for a in addrs if a.get("type") == 2), None) or {}
    street = " ".join(x for x in [addr.get("street"), addr.get("buildingNumber"), addr.get("entrance"),
                                  addr.get("apartmentNumber")] if x) or (f"PL {addr['postOfficeBox']}" if addr.get("postOfficeBox") else None)
    city = next((p.get("city") for p in addr.get("postOffices") or [] if p.get("languageCode") == "1"),
                (addr.get("postOffices") or [{}])[0].get("city") if addr.get("postOffices") else None)
    entries = c.get("registeredEntries") or []
    active = lambda reg, typ=None: any(e.get("register") == reg and (typ is None or e.get("type") == typ)  # noqa: E731
                                       and not e.get("endDate") for e in entries)
    situations = [{"type": s.get("type"), "label": SITUATIONS.get(s.get("type"), s.get("type")),
                   "since": s.get("registrationDate")} for s in c.get("companySituations") or [] if not s.get("endDate")]
    return {
        "found": True, "business_id": (c.get("businessId") or {}).get("value") or bid, "name": main.get("name"),
        "auxiliary_names": aux,
        "company_form": _desc(((c.get("companyForms") or [{}])[0]).get("descriptions")),
        "status_code": c.get("status"), "business_id_status": {"1": "pending", "2": "valid", "5": "invalidated"}.get(c.get("status"), c.get("status")),
        "trade_register_status": {"0": "unregistered", "1": "registered", "2": "removed", "3": "start-up not registered",
                                  "4": "ceased"}.get(c.get("tradeRegisterStatus"), c.get("tradeRegisterStatus")),
        "registration_date": c.get("registrationDate"), "end_date": c.get("endDate"),
        "main_business_line": _desc((c.get("mainBusinessLine") or {}).get("descriptions")),
        "address": {"street": street, "postal_code": addr.get("postCode"), "city": city.title() if city else None,
                    "country": "FI", "co": addr.get("co") or None},
        "vat_registered": active("6", "80"), "employer_register": active("7"), "prepayment_register": active("5"),
        "situations": situations, "website": (c.get("website") or {}).get("url"), "last_modified": c.get("lastModified"),
        "source": "PRH/Vero YTJ open data API v3", "url": url, "fetched_at": now_iso(),
    }


# ---------------------------------------------------------------------------------------------
# VIES
# ---------------------------------------------------------------------------------------------
async def vies_check(vat_id: str) -> dict:
    s = re.sub(r"[\s.-]", "", vat_id or "").upper()
    m = re.fullmatch(r"([A-Z]{2})([0-9A-Z]{2,12})", s)
    if not m:
        return {"status": "fail", "valid": False, "summary": f"'{vat_id}' is not a VAT number", "checked_at": now_iso()}
    ms, num = m.group(1), m.group(2)
    url = VIES_URL.format(ms="EL" if ms == "GR" else ms, num=num)
    try:
        r = await recorded_http.request("GET", url, headers=UA, timeout=25)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "summary": f"VIES did not answer ({type(exc).__name__}); retry later",
                "url": url, "checked_at": now_iso(), "source": "EU VIES"}
    err = data.get("userError")
    if err and err not in ("VALID", "INVALID"):
        return {"status": "error", "summary": f"VIES could not check the number right now ({err}); retry later",
                "url": url, "checked_at": now_iso(), "source": "EU VIES", "data": {"userError": err}}
    valid = bool(data.get("isValid"))
    return {"status": "pass" if valid else "fail", "valid": valid, "vat_id": f"{ms}{num}",
            "summary": (f"{ms}{num} is a valid VAT number: {data.get('name') or '—'}" if valid
                        else f"{ms}{num} is not an active VAT number in VIES"),
            "data": {"name": data.get("name"), "address": data.get("address"), "request_date": data.get("requestDate")},
            "source": "EU VIES", "url": url, "checked_at": now_iso()}


# ---------------------------------------------------------------------------------------------
# EORI (EU EOS)
# ---------------------------------------------------------------------------------------------
def eori_candidates(eori: str | None, business_id: str | None = None) -> list[str]:
    out = []
    s = re.sub(r"\s", "", eori or "").upper()
    m = re.fullmatch(r"FI(\d{7})-?(\d)", s)
    if m:
        out += [f"FI{m.group(1)}-{m.group(2)}", f"FI{m.group(1)}{m.group(2)}"]
    elif s:
        out.append(s)
    bid = normalise_business_id(business_id)
    if bid and not out:
        out += [f"FI{bid}", f"FI{bid.replace('-', '')}"]
    return list(dict.fromkeys(out))


async def eori_check(eori: str | None, business_id: str | None = None) -> dict:
    cands = eori_candidates(eori, business_id)
    if not cands:
        return {"status": "warn", "summary": "No EORI number given. Apply in Tulli's EORI Number Registration Service "
                                             "before the first customs declaration.", "checked_at": now_iso(),
                "source": "EU EOS EORI validation"}
    body = ("<soapenv:Envelope xmlns:soapenv=\"http://schemas.xmlsoap.org/soap/envelope/\" "
            "xmlns:ev=\"http://eori.ws.eos.dds.s/\"><soapenv:Header/><soapenv:Body><ev:validateEORI>"
            + "".join(f"<ev:eori>{c}</ev:eori>" for c in cands) + "</ev:validateEORI></soapenv:Body></soapenv:Envelope>")
    try:
        r = await recorded_http.request("POST", EOS_URL, content=body.encode(), timeout=25,
                                        headers={**UA, "Content-Type": "text/xml; charset=utf-8"})
        root = ET.fromstring(r.content)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "summary": f"The EU EORI service did not answer ({type(exc).__name__}); retry later",
                "checked_at": now_iso(), "source": "EU EOS EORI validation", "url": EOS_URL}
    results = []
    for res in root.iter("result"):
        g = lambda tag: (res.find(tag).text if res.find(tag) is not None else None)  # noqa: E731
        results.append({"eori": g("eori"), "status": g("status"), "status_text": g("statusDescr"), "name": g("name"),
                        "street": g("street"), "postal_code": g("postalCode"), "city": g("city"), "country": g("country")})
    valid = next((x for x in results if x["status"] == "0"), None)
    if valid:
        return {"status": "pass", "valid": True, "eori": valid["eori"],
                "summary": f"{valid['eori']} is valid in the EU EORI registry" + (f": {valid['name']}" if valid.get("name") else ""),
                "data": valid, "tried": cands, "source": "EU EOS EORI validation", "url": EOS_URL, "checked_at": now_iso()}
    return {"status": "fail" if eori else "warn", "valid": False, "eori": cands[0], "tried": cands,
            "summary": (f"{cands[0]} is not in the EU EORI registry. Check the number, or apply in Tulli's EORI Number "
                        f"Registration Service." if eori else
                        f"No EORI registered for {cands[0]}. Apply in Tulli's EORI Number Registration Service before "
                        f"the first customs declaration."),
            "data": {"results": results}, "source": "EU EOS EORI validation", "url": EOS_URL, "checked_at": now_iso()}


# ---------------------------------------------------------------------------------------------
# KYC run and decision
# ---------------------------------------------------------------------------------------------
def poa_valid(company: dict) -> bool:
    poa = company.get("poa") or {}
    today = now_iso()[:10]
    return bool(poa.get("valid_to") and poa["valid_to"] >= today and poa.get("valid_from", "") <= today
                and not poa.get("revoked_at") and poa.get("signed_at"))


def _fi_num(n) -> str:
    return f"{n:,.0f}".replace(",", " ")


def fi_summary(key: str, chk: dict, company: dict) -> str | None:
    """Finnish text for a KYC check result, built from the structured result (the English summary stays as is)."""
    st = chk.get("status")
    data = chk.get("data") or {}
    if key == "ytunnus":
        bid = normalise_business_id(company.get("business_id")) or company.get("business_id")
        return f"{bid}: tarkistusnumero on oikein" if st == "pass" else f"{bid}: tarkistusnumero on väärä"
    if key == "ytj":
        if st == "error":
            return "YTJ ei vastannut; yritä myöhemmin uudelleen"
        if not data:
            return f"{company.get('business_id')} ei löydy YTJ-yritystietojärjestelmästä"
        probs = []
        if data.get("business_id_status") != "valid":
            probs.append(f"Y-tunnuksen tila: {data.get('business_id_status')}")
        if data.get("trade_register_status") not in ("registered",):
            probs.append(f"kaupparekisterin tila: {data.get('trade_register_status')}")
        if data.get("end_date"):
            probs.append(f"toiminta päättynyt {data['end_date']}")
        for x in data.get("situations") or []:
            probs.append({"restructuring": "yrityssaneeraus", "liquidation": "selvitystila", "bankruptcy": "konkurssi"}
                         .get(x.get("label"), x.get("label") or "") + f" alkaen {x.get('since') or '—'}")
        if company.get("name") and not _same_name(company.get("name"), data.get("name")):
            probs.append(f"rekisteröity nimi on '{data.get('name')}', profiilissa '{company.get('name')}'")
        return "; ".join(probs) if probs else f"{data.get('name')}: rekisteröity {data.get('registration_date') or '—'}, Y-tunnus voimassa"
    if key == "vies":
        if st == "pass":
            return f"{chk.get('vat_id')} on voimassa oleva ALV-numero (VIES): {data.get('name') or '—'}"
        if st == "not_applicable":
            return "Ei arvonlisäverorekisterissä (YTJ); maahantuonnin arvonlisävero maksetaan Tullille"
        if st == "error":
            return "VIES ei vastannut; yritä myöhemmin uudelleen"
        return f"{chk.get('vat_id') or company.get('vat_id') or ''} ei ole aktiivinen ALV-numero VIES-järjestelmässä".strip()
    if key == "eori":
        e = chk.get("eori") or company.get("eori")
        if st == "pass":
            return f"{e} on voimassa EU:n EORI-rekisterissä"
        if st == "manual":
            return f"EORI {e} vahvistettu käsin ({(chk.get('by') or {}).get('name')}, {(chk.get('at') or '')[:10]}): {chk.get('note')}"
        if st == "error":
            return "EU:n EORI-palvelu ei vastannut; yritä myöhemmin uudelleen"
        if st == "warn":
            return "EORI-numeroa ei ole. Hae se Tullin EORI-numeron rekisteröintipalvelusta ennen ensimmäistä tulli-ilmoitusta."
        return f"{e} ei löydy EU:n EORI-rekisteristä. Tarkista numero tai hae EORI-numero Tullista."
    if key == "sanctions":
        n = len(chk.get("screened_names") or [])
        if st == "pass":
            cleared = [h for h in chk.get("hits") or [] if h.get("previously_cleared")]
            return (f"{len(cleared)} osumaa todettu aiemmin vääriksi hälytyksiksi" if cleared
                    else f"Ei osumia {n} nimelle EU:n, YK:n ja OFACin pakotelistoilla")
        if st == "fail":
            return f"{len(chk.get('hits') or [])} mahdollista osumaa pakotelistoilla – tarkistetaan käsin"
        return "Pakotelistatarkistus ei valmistunut; yritä myöhemmin uudelleen"
    if key == "ubo":
        ubos = company.get("ubos") or []
        peps = [u["name"] for u in ubos if u.get("pep")]
        if ubos or company.get("ubo_none_declared"):
            base = (f"{len(ubos)} tosiasiallista edunsaajaa ilmoitettu" if ubos
                    else "Ilmoitettu: kenelläkään ei ole yli 25 %:n omistusta tai määräysvaltaa")
            return base + (f"; poliittisesti vaikutusvaltainen: {', '.join(peps)}" if peps else "") + \
                (f"; {chk['registry_fi']}" if chk.get("registry_fi") else "")
        return "Tosiasiallisia edunsaajia (> 25 %) ei ole vielä ilmoitettu" + \
            (f"; {chk['registry_fi']}" if chk.get("registry_fi") else "")
    if key == "pep":
        return chk.get("summary_fi")
    if key == "poa":
        poa = company.get("poa") or {}
        if st == "pass":
            return f"{'Suora' if poa.get('type') == 'direct' else 'Epäsuora'} edustus, voimassa {poa.get('valid_to')} asti"
        return "Tulliedustuksen valtakirjaa ei ole vielä allekirjoitettu"
    return None


async def run_kyc(company_id: str, actor: dict | None) -> dict:
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise HTTPException(404, "Company not found")
    bid = normalise_business_id(company.get("business_id"))
    if not bid:
        raise HTTPException(400, "Add the company's Business ID (Y-tunnus) first")
    kyc = dict(company.get("kyc") or {})
    started = now_iso()
    prev_status = company.get("kyc_status") if company.get("kyc_status") != "pending" else \
        (company.get("kyc") or {}).get("prev_status") or "not_started"
    await db.companies.update_one({"id": company_id}, {"$set": {"kyc_status": "pending", "kyc.run_started_at": started,
                                                                "kyc.prev_status": prev_status}})
    try:
        return await _run_kyc(company, company_id, bid, kyc, actor)
    except BaseException:
        await db.companies.update_one({"id": company_id, "kyc_status": "pending"},
                                      {"$set": {"kyc_status": prev_status, "kyc_stale": True}})
        raise


async def _run_kyc(company: dict, company_id: str, bid: str, kyc: dict, actor: dict | None) -> dict:
    from services import screening


    checks: dict[str, dict] = {}
    checks["ytunnus"] = {"status": "pass" if ytunnus_valid(bid) else "fail", "checked_at": now_iso(),
                         "summary": f"{bid} has a valid check digit" if ytunnus_valid(bid) else f"{bid} has an invalid check digit",
                         "source": "Business ID check digit (weights 7,9,10,5,8,4,2, mod 11)"}
    ytj = None
    from core.sandbox import sandbox_eori, sandbox_registers, sandbox_vies, sandbox_ytj
    sandbox = sandbox_registers(company)
    try:
        ytj = sandbox_ytj(company, bid) if sandbox else await ytj_lookup(bid)
        if not ytj.get("found"):
            checks["ytj"] = {"status": "fail", "summary": f"{bid} was not found in the YTJ business register",
                             "source": "PRH/Vero YTJ open data API v3", "url": ytj.get("url"), "checked_at": now_iso()}
        else:
            problems = []
            if ytj["business_id_status"] != "valid":
                problems.append(f"Business ID status is {ytj['business_id_status']}")
            if ytj["trade_register_status"] not in ("registered",):
                problems.append(f"trade register status is {ytj['trade_register_status']}")
            if ytj.get("end_date"):
                problems.append(f"ended {ytj['end_date']}")
            for s in ytj.get("situations") or []:
                problems.append(f"{s['label']} since {s.get('since') or '—'}")
            name_ok = not company.get("name") or _same_name(company.get("name"), ytj.get("name"))
            if not name_ok:
                problems.append(f"registered name is '{ytj.get('name')}', profile says '{company.get('name')}'")
            checks["ytj"] = {"status": "pass" if not problems else ("fail" if ytj.get("situations") or ytj.get("end_date")
                                                                    else "warn"),
                             "summary": (f"{ytj['name']}: {ytj.get('company_form') or ''}, registered "
                                         f"{ytj.get('registration_date') or '—'}, Business ID valid") if not problems
                             else "; ".join(problems),
                             "data": ytj, "source": ytj["source"], "url": ytj["url"], "checked_at": ytj["fetched_at"]}
    except HTTPException as exc:
        checks["ytj"] = {"status": "error", "summary": exc.detail, "checked_at": now_iso(),
                         "source": "PRH/Vero YTJ open data API v3"}
    vat_registered = (ytj or {}).get("vat_registered") if ytj and ytj.get("found") else company.get("vat_registered")
    vat_id = company.get("vat_id") or f"FI{bid.replace('-', '')}"
    if vat_registered or company.get("vat_id"):
        checks["vies"] = sandbox_vies(vat_id) if sandbox else await vies_check(vat_id)
        if checks["vies"]["status"] == "fail" and not vat_registered:
            checks["vies"]["status"] = "warn"
    else:
        checks["vies"] = {"status": "not_applicable", "summary": "Not in the VAT register (YTJ); import VAT is payable to "
                                                                 "Customs", "checked_at": now_iso(), "source": "EU VIES"}
    checks["eori"] = sandbox_eori(bid, company.get("eori")) if sandbox else await eori_check(company.get("eori"), None)
    if not company.get("eori") and not sandbox:
        probe = await eori_check(None, bid)
        if probe.get("status") == "pass":
            checks["eori"] = {**probe, "summary": probe["summary"] + " (found from the Business ID; saved to the profile)"}
    prev_eori = (company.get("kyc") or {}).get("eori") or {}
    if manual_eori_applies(prev_eori, company.get("eori")) and checks["eori"].get("status") != "pass":
        # a broker's documented verification stands until the registry confirms the number (or the number changes)
        checks["eori"] = {**prev_eori, "registry_result": {**checks["eori"],
                                                           "summary_fi": fi_summary("eori", checks["eori"], company)}}
    # sanctions: company + auxiliary names + UBOs + signatory
    parties = [{"name": company.get("name"), "kind": "entity", "country": "FI", "role": "company"}]
    for n in ((ytj or {}).get("auxiliary_names") or [])[:5]:
        parties.append({"name": n, "kind": "entity", "country": None, "role": "auxiliary name"})
    for u in company.get("ubos") or []:
        if u.get("name"):
            parties.append({"name": u["name"], "kind": "person", "country": u.get("country"), "role": "beneficial owner"})
    sig = company.get("signatory") or {}
    if sig.get("name"):
        parties.append({"name": sig["name"], "kind": "person", "country": None, "role": "signatory"})
    try:
        scr = await screening.screen_parties(parties)
    except Exception as exc:  # noqa: BLE001
        log.exception("screening failed")
        scr = {"status": "error", "results": [], "error": str(exc)[:200]}
    hits = [{"role": r.get("role"), "query": r.get("query"), **h} for r in scr.get("results") or [] for h in r.get("hits") or []]
    lists = next((r.get("lists") for r in scr.get("results") or [] if r.get("lists")), [])
    # A compliance officer's "false positive" decision covers exactly the (screened name, list, entry) pairs they saw.
    # A new name, a new list entry or a changed list reference is screened afresh.
    cleared = {tuple(x) for x in (company.get("kyc") or {}).get("cleared_hits") or []}
    for h in hits:
        h["previously_cleared"] = _hit_key(h) in cleared
    if scr.get("status") == "potential_match" and hits and all(h["previously_cleared"] for h in hits):
        scr["status"] = "clear"
        scr["cleared_note"] = (f"{len(hits)} match(es) were cleared earlier as false positives by "
                               f"{((company.get('kyc') or {}).get('sanctions_decision') or {}).get('by', {}).get('name') or 'compliance'}")
    checks["sanctions"] = {"status": {"clear": "pass", "potential_match": "fail"}.get(scr.get("status"), "error"),
                           "summary": (scr["cleared_note"] if scr.get("cleared_note") else
                                       f"No match for {len(parties)} name(s) on the EU, UN and OFAC lists" if scr.get("status") == "clear"
                                       else f"{len(hits)} potential match(es): " + "; ".join(
                                           f"{h.get('query')} ~ {h.get('matched_name')} ({h.get('list', '').upper()} {h.get('ref') or ''}, "
                                           f"score {h.get('score', 0):.2f})" for h in hits[:3])
                                       if scr.get("status") == "potential_match" else
                                       "Sanctions screening could not complete: " + (scr.get("error") or "lists not loaded")),
                           "hits": hits[:20], "lists": lists, "screened_names": [p["name"] for p in parties],
                           "source": "EU consolidated financial sanctions list, UN consolidated list, OFAC SDN",
                           "checked_at": scr.get("screened_at") or now_iso()}
    ubos = company.get("ubos") or []
    peps = [u for u in ubos if u.get("pep")]
    if ubos or company.get("ubo_none_declared"):
        checks["ubo"] = {"status": "warn" if peps else "pass", "checked_at": now_iso(),
                         "summary": (f"{len(ubos)} beneficial owner(s) declared" if ubos else
                                     "Declared: no person owns or controls more than 25 %")
                         + (f"; politically exposed: {', '.join(u['name'] for u in peps)}" if peps else ""),
                         "source": "Customer declaration (compare with the PRH beneficial owner register)"}
    else:
        checks["ubo"] = {"status": "warn", "summary": "Beneficial owners (> 25 %) not declared yet", "checked_at": now_iso(),
                         "source": "Customer declaration"}
    # s12: registry owners / parents next to the declaration, and the PEP check of every person
    from services import beneficial_owners, pep as pep_svc

    owners = await beneficial_owners.owners_for_company(company, ytj=ytj)
    checks["ubo"] = beneficial_owners.ubo_with_registry(checks["ubo"], owners)
    checks["pep"] = await pep_svc.kyc_check(company, owners)
    checks["poa"] = {"status": "pass" if poa_valid(company) else "warn", "checked_at": now_iso(),
                     "summary": (f"{(company.get('poa') or {}).get('type')} representation, valid to "
                                 f"{(company.get('poa') or {}).get('valid_to')}") if poa_valid(company)
                     else "No signed power of attorney for customs representation yet", "source": "Signed in the portal"}

    for k, v in checks.items():
        v["summary_fi"] = fi_summary(k, v, company)
        kyc[k] = v
    risk = {k: checks[k]["status"] for k in RISK_CHECKS}
    green = all(s in ("pass", "not_applicable") for s in risk.values())
    settings = await get_settings()
    sanctions_status = {"pass": "clear", "fail": "potential_match"}.get(checks["sanctions"]["status"], company.get("sanctions_status") or "not_screened")
    prev = company.get("kyc_status")
    # One decision gate (core.agent_runtime.decide): low risk is approved by the KYC agent only while Autopilot is on
    # and auto_approve_low_risk_kyc allows it; everything else becomes an owner-queue card with its reasons.
    from services.agents.kyc_agent import gate_kyc_run
    gate = None
    if company.get("kyc_status") == "rejected" and not green:
        new_status = "rejected"
    else:
        gate = await gate_kyc_run(company, checks, risk, green, sanctions_status, settings, actor)
        new_status = "approved" if gate.auto else "in_review"
    kyc.update({"run_at": now_iso(), "run_by": actor_ref(actor), "risk": risk, "green": green,
                "sandbox": bool(sandbox) or None, "agent_decision_id": gate.id if gate else None})
    if new_status == "approved":
        from core.agent_runtime import agent_bot
        bot = agent_bot("kyc_agent")
        kyc.update({"decision": "approved", "decided_by": {"type": "agent", "id": bot["id"], "name": bot["name"]},
                    "decided_at": now_iso(), "note": "Low risk: every check passed"})
    sets = {"kyc": kyc, "kyc_status": new_status, "sanctions_status": sanctions_status, "kyc_checked_at": now_iso(),
            "beneficial_owners": owners}
    if ytj and ytj.get("found"):
        sets["vat_registered"] = bool(ytj.get("vat_registered"))
        sets["ytj"] = {k: ytj.get(k) for k in ("name", "company_form", "registration_date", "main_business_line",
                                                  "address", "fetched_at", "url", "trade_register_status")}
        if not company.get("vat_id") and ytj.get("vat_registered"):
            sets["vat_id"] = f"FI{bid.replace('-', '')}"
    if checks["eori"].get("status") == "pass" and checks["eori"].get("eori"):
        sets["eori"] = checks["eori"]["eori"]
        sets["eori_validated_at"] = now_iso()
    if checks["vies"].get("status") == "pass":
        sets["vies_checked_at"] = now_iso()
    await db.companies.update_one({"id": company_id}, {"$set": sets})
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    await audit(actor, "kyc_run", "company", company_id, {"status": new_status, "risk": risk}, company_id=company_id)
    if new_status == "in_review":
        failing = [f"{k}: {checks[k]['summary']}" for k, s in risk.items() if s not in ("pass", "not_applicable")]
        card = gate.approval if (gate and gate.approval) else None  # the gate files the card (s2)
        if not card:  # old path for a rejected re-run
            card = await request_approval("kyc_review", "company", company_id, f"KYC review: {company.get('name')}",
                                          summary="; ".join(failing)[:900], reason="Not every KYC / sanctions check is green",
                                          company_id=company_id, requested_by=actor, link=f"/ops/customers/{company_id}",
                                          data={"risk": risk, "sanctions_status": sanctions_status})
        if checks["pep"]["status"] == "review":  # s12: PEP relationship is a legal point, a person decides
            await pep_svc.mark_card(card, company, checks["pep"])
        failing_fi = [f"{CHECK_FI.get(k, k)}: {checks[k].get('summary_fi') or checks[k]['summary']}"
                      for k, s in risk.items() if s not in ("pass", "not_applicable")]
        await notify_role(["ops_admin"], bi(f"Asiakastuntemuksen tarkistus: {company.get('name')}", f"KYC review: {company.get('name')}"),
                          bi("; ".join(failing_fi)[:300], "; ".join(failing)[:300]), link=f"/ops/customers/{company_id}")
    else:
        await cancel_approvals(company_id, "kyc_review")
    if prev != new_status:
        await emit("company.kyc_changed", {"company": company, "actor": actor})
    return company


def _same_name(a: str | None, b: str | None) -> bool:
    from services.documents import party_similarity

    return party_similarity(a, b) >= 0.85


def manual_eori_applies(check: dict | None, eori: str | None) -> bool:
    """A manual EORI verification covers exactly the number that was verified."""
    from services.customs import eori_normalise

    return bool(check and check.get("status") == "manual" and eori and eori_normalise(check.get("eori")) == eori_normalise(eori))


async def verify_eori_manually(company_id: str, actor: dict, note: str, document_id: str | None = None,
                               revoke: bool = False) -> dict:
    """Audited broker override for E-1 when EU EOS does not confirm the importer's EORI (EOS outage, a newly issued
    number not yet propagated, Tulli's EORI decision in hand). Bound to the current EORI number."""
    from services.customs import eori_normalise

    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise HTTPException(404, "Company not found")
    eori = eori_normalise(company.get("eori"))
    if not eori:
        raise HTTPException(400, "The company has no EORI number to verify")
    prev = (company.get("kyc") or {}).get("eori") or {}
    if revoke:
        if prev.get("status") != "manual":
            raise HTTPException(409, "There is no manual EORI verification to revoke")
        new = prev.get("registry_result") or await eori_check(eori)
        new = {**new, "summary_fi": new.get("summary_fi") or fi_summary("eori", new, company)}
        action = "eori_manual_revoked"
    else:
        if prev.get("status") == "pass":
            raise HTTPException(409, f"{eori} is already confirmed by the EU EORI registry")
        if document_id:
            d = await db.documents.find_one({"id": document_id}, {"_id": 0, "company_id": 1})
            if not d or d.get("company_id") != company_id:
                raise HTTPException(400, "The evidence document must belong to this customer")
        by = actor_ref(actor)
        new = {"status": "manual", "valid": True, "eori": eori, "by": by, "at": now_iso(), "note": note.strip(),
               "summary_fi": f"EORI {eori} vahvistettu käsin ({by.get('name')}, {now_iso()[:10]}): {note.strip()}",
               "document_id": document_id, "checked_at": now_iso(), "source": "Broker verification (manual)",
               "summary": f"EORI {eori} verified manually by {by.get('name')} on {now_iso()[:10]}: {note.strip()}",
               "registry_result": {**{k: prev.get(k) for k in ("status", "summary", "checked_at", "tried", "source", "url",
                                                               "eori")},
                                   "summary_fi": prev.get("summary_fi") or fi_summary("eori", prev, company)}
               if prev else None}
        action = "eori_verified_manually"
    await db.companies.update_one({"id": company_id}, {"$set": {"kyc.eori": new}})
    await audit(actor, action, "company", company_id, {"eori": eori, "note": note, "document_id": document_id},
                company_id=company_id)
    # open declarations re-validate E-1 now
    from services import customs as customs_svc

    async for d in db.customs_declarations.find({"company_id": company_id,
                                                 "status": {"$in": ["draft", "needs_info", "ready_for_review"]}}, NO_ID):
        await customs_svc.save_and_recompute(d, actor, "revalidated", {"reason": action})
    return await db.companies.find_one({"id": company_id}, NO_ID)


def _hit_key(h: dict) -> tuple:
    return ((h.get("query") or "").strip().lower(), h.get("list") or "", str(h.get("ref") or h.get("matched_name") or ""))


async def decide_kyc(company_id: str, decision: str, actor: dict, note: str | None = None,
                     sanctions_decision: str | None = None) -> dict:
    if decision not in ("approved", "rejected", "in_review"):
        raise HTTPException(400, "Decision must be approved, rejected or in_review")
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    if not company:
        raise HTTPException(404, "Company not found")
    if decision == "approved" and company.get("sanctions_status") in ("potential_match", "blocked") and sanctions_decision != "clear":
        raise HTTPException(400, "This customer has a potential sanctions match: record the sanctions decision "
                                 "(cleared as a false positive, or blocked) before approving KYC (RULE K-1)")
    if sanctions_decision and sanctions_decision not in ("clear", "blocked"):
        raise HTTPException(400, "Sanctions decision is clear or blocked")
    if decision == "approved" and sanctions_decision == "blocked":
        raise HTTPException(400, "A customer blocked for sanctions cannot be approved: reject KYC instead")
    if decision != "in_review" and not (note or "").strip() and (company.get("kyc") or {}).get("green") is False:
        raise HTTPException(400, "Write a short reason: some checks were not green")
    pep_chk = (company.get("kyc") or {}).get("pep") or {}
    if decision == "approved" and pep_chk.get("status") == "review" and not _is_person(actor):
        raise HTTPException(403, "A relationship with a politically exposed person needs a person's approval "
                                 "(legal point pep_relationship); an agent cannot approve it")
    prev = company.get("kyc_status")
    sets = {"kyc_status": decision, "kyc.decision": decision, "kyc.decided_by": actor_ref(actor),
            "kyc.decided_at": now_iso(), "kyc.note": note}
    if decision == "approved" and pep_chk.get("status") == "review":
        old = set(((company.get("kyc") or {}).get("pep_decision") or {}).get("approved") or [])
        sets["kyc.pep_decision"] = {"approved": sorted(old | set(pep_chk.get("hit_keys") or [])), "by": actor_ref(actor),
                                    "at": now_iso(), "note": note}
        sets["kyc.pep.status"] = "pass"
        sets["kyc.pep.approved"] = True
        sets["kyc.pep.summary"] = f"PEP relationship approved by {actor_ref(actor).get('name')}: {pep_chk.get('summary')}"
        sets["kyc.pep.summary_fi"] = f"PEP-asiakkuuden hyväksyi {actor_ref(actor).get('name')}: {pep_chk.get('summary_fi')}"
    if sanctions_decision:
        sets["sanctions_status"] = sanctions_decision
        sets["kyc.sanctions_decision"] = {"decision": sanctions_decision, "by": actor_ref(actor), "at": now_iso(),
                                          "note": note}
        hits = ((company.get("kyc") or {}).get("sanctions") or {}).get("hits") or []
        if sanctions_decision == "clear":
            sets["kyc.cleared_hits"] = sorted({_hit_key(h) for h in hits} | {tuple(x) for x in (company.get("kyc") or {})
                                                                               .get("cleared_hits") or []})
    if decision == "rejected" or sanctions_decision == "blocked":
        sets["status"] = "blocked" if sanctions_decision == "blocked" else company.get("status", "active")
    await db.companies.update_one({"id": company_id}, {"$set": sets})
    pend = await db.approvals.find_one({"kind": "kyc_review", "ref_id": company_id, "status": "pending"}, NO_ID)
    if pend and decision != "in_review":
        await db.approvals.update_one({"id": pend["id"]}, {"$set": {
            "status": decision, "decided_at": now_iso(), "decided_by": {"id": actor.get("id"), "name": actor.get("name")},
            "decision_note": note}})
    company = await db.companies.find_one({"id": company_id}, NO_ID)
    await audit(actor, f"kyc_{decision}", "company", company_id, {"note": note, "sanctions": sanctions_decision},
                company_id=company_id)
    if prev != decision:
        await emit("company.kyc_changed", {"company": company, "actor": actor})
        await notify_company(company_id, bi("Yrityksen tunnistus " + ("hyväksytty" if decision == "approved" else
                                                                     "vaatii huomiota" if decision == "in_review" else "hylätty"),
                                            "Company verification " + ("approved" if decision == "approved" else
                                                                       "needs attention" if decision == "in_review" else "declined")),
                             same(note or ""), link="/portal/company", admins_only=True)
    return company


@on("approval.decided")
async def _on_kyc_approval(p):
    a, actor = p["approval"], p.get("actor") or {}
    if a.get("kind") != "kyc_review":
        return
    company = await db.companies.find_one({"id": a["ref_id"]}, NO_ID)
    if not company or company.get("kyc_status") == a["status"]:
        return
    sd = (a.get("decision_data") or {}).get("sanctions_decision")
    if a["status"] == "approved" and ((company.get("kyc") or {}).get("pep") or {}).get("status") == "review" \
            and not _is_person(actor):
        # legal point pep_relationship: an agent's (or the system's) approval does not count; back to the owner queue
        await db.approvals.update_one({"id": a["id"]}, {"$set": {"status": "pending", "decided_at": None, "decided_by": None,
                                                                 "decision_note": "A PEP relationship needs a person's approval"}})
        return
    if a["status"] == "approved" and company.get("sanctions_status") in ("potential_match", "blocked") and sd != "clear":
        # approving from the generic queue without a sanctions decision is not allowed: keep it in review
        await db.approvals.update_one({"id": a["id"]}, {"$set": {"status": "pending", "decided_at": None, "decided_by": None,
                                                                 "decision_note": "Needs a sanctions decision on the customer page"}})
        return
    await decide_kyc(a["ref_id"], a["status"], actor, a.get("decision_note") or "Decided in the approvals queue", sd)


def _is_person(actor: dict | None) -> bool:
    return bool(actor and actor.get("id") and actor.get("type") not in ("agent", "system"))


def stale_pending(company: dict, minutes: int = 3) -> bool:
    """A KYC run that never finished (process restarted mid-run) is reported as stale, not as running forever."""
    from datetime import datetime, timezone

    if company.get("kyc_status") != "pending":
        return False
    started = (company.get("kyc") or {}).get("run_started_at")
    try:
        t = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - t).total_seconds() > minutes * 60
