"""Beneficial owners and corporate parents of a customer company from registry sources, next to its own declaration.

    await owners_for_company(company, ytj=None)
    -> {sources_checked[{source, status: ok|not_configured|not_available|error, detail}],
        owners[{name, share_pct?, share_band?, source, kind: person|entity, country?, birth_date?}],
        parents[{lei, name, relation: direct|ultimate, source: "GLEIF", ...}], lei, reporting_exceptions[], declared[...],
        checked_at}

Sources (SOURCES-owners-pep.md has the access and licence findings):
  GLEIF        LEI API https://api.gleif.org/api/v1 (CC0, free, live): LEI by registeredAs (Finnish Business ID) and
               country, then Level 2 direct-parent / ultimate-parent relationship records and reporting exceptions.
  PRH          beneficial owner register (kaupparekisteri, tosiasialliset edunsaajat). PRH discloses it only to users
               with an AML-law purpose (rahanpesulaki), via the Edunsaajatietopalvelu (Suomi.fi login) or, for contract
               customers, the X-Road service XRoadBeneficialOwnerCompanyRepresentInfoV2 (published WSDL in the
               Suomi.fi liityntäkatalogi, FI/GOV/0244683-1/xroadvirre-services). PRHBeneficialOwnersAdapter implements
               that SOAP call; it needs Suomi.fi-palveluväylä membership (own security server) and a PRH contract:
               PRH_BO_API_URL (the security server endpoint), PRH_BO_CLIENT_ID (X-Road client
               instance/memberClass/memberCode/subsystem) and PRH_BO_USER_ID (PRH user id). PRH_BO_CLIENT_SECRET is
               not part of X-Road (the security server authenticates with its certificate) and is not read.
  YTJ          open data API v3 (CC BY 4.0): company form and register entries only; it has no owners, board or
               representatives, so it never yields owners (reported as not_available for ownership).
  CompaniesHouse  UK PSC register (free key, COMPANIES_HOUSE_API_KEY) for UK counterparties with a company number.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET

import httpx

from core.util import now_iso

log = logging.getLogger("beneficial_owners")

GLEIF = "https://api.gleif.org/api/v1"
CH_API = "https://api.company-information.service.gov.uk"
UA = {"User-Agent": "HuolitsijaCompliance/1.0 (https://huolitsija.fi; compliance@huolitsija.fi)"}
PRH_SERVICE = "FI/GOV/0244683-1/xroadvirre-services/XRoadBeneficialOwnerCompanyRepresentInfoV2/v1"
NS = {"soapenv": "http://schemas.xmlsoap.org/soap/envelope/", "xrd": "http://x-road.eu/xsd/xroad.xsd",
      "id": "http://x-road.eu/xsd/identifiers", "prod": "http://xroad.virre.prh.fi/producer",
      "esr": "http://www.prh.fi/ESRServices"}


def _src(source: str, status: str, detail: str, **kw) -> dict:
    return {"source": source, "status": status, "detail": detail, "checked_at": now_iso(), **kw}


# ---------------------------------------------------------------------------------------------
# GLEIF (live, CC0)
# ---------------------------------------------------------------------------------------------
def _lei_brief(rec: dict) -> dict:
    a = rec.get("attributes") or {}
    e = a.get("entity") or {}
    return {"lei": rec.get("id"), "name": (e.get("legalName") or {}).get("name"), "country": (e.get("legalAddress") or {}).get("country"),
            "jurisdiction": e.get("jurisdiction"), "registered_as": e.get("registeredAs"), "status": e.get("status"),
            "registration_status": (a.get("registration") or {}).get("status"),
            "url": f"https://search.gleif.org/#/record/{rec.get('id')}"}


async def gleif_lookup(business_id: str | None, name: str | None = None, country: str | None = "FI") -> dict:
    from services.demo_world.boundary import registry_http

    async def get(client, url, **kwargs):
        target = str(httpx.URL(url, params=kwargs.get("params")))
        response = await registry_http("GET", target)
        return response if response is not None else await client.get(url, **kwargs)

    async with httpx.AsyncClient(timeout=25, headers={**UA, "Accept": "application/vnd.api+json"}) as c:
        rec = None
        if business_id:
            q = {"filter[entity.registeredAs]": business_id}
            if country:
                q["filter[entity.legalAddress.country]"] = country
            r = await get(c, f"{GLEIF}/lei-records", params=q)
            r.raise_for_status()
            data = r.json().get("data") or []
            rec = data[0] if data else None
        if rec is None and name and not business_id:
            r = await get(c, f"{GLEIF}/lei-records", params={"filter[entity.legalName]": name,
                                                             **({"filter[entity.legalAddress.country]": country} if country else {})})
            r.raise_for_status()
            data = r.json().get("data") or []
            rec = data[0] if len(data) == 1 else None
        if rec is None:
            return {"found": False}
        lei = rec["id"]
        parents, exceptions = [], []
        for rel in ("direct", "ultimate"):
            r = await get(c, f"{GLEIF}/lei-records/{lei}/{rel}-parent-relationship")
            if r.status_code == 200:
                ra = (r.json().get("data") or {}).get("attributes") or {}
                rr = ra.get("relationship") or {}
                pr = await get(c, f"{GLEIF}/lei-records/{lei}/{rel}-parent")
                pb = _lei_brief(pr.json()["data"]) if pr.status_code == 200 else {"lei": (rr.get("endNode") or {}).get("id")}
                parents.append({**pb, "relation": rel, "source": "GLEIF", "relationship_type": rr.get("type"),
                                "relationship_status": rr.get("status"),
                                "corroboration": (ra.get("registration") or {}).get("corroborationLevel")})
                continue
            r = await get(c, f"{GLEIF}/lei-records/{lei}/{rel}-parent-reporting-exception")
            if r.status_code == 200:
                ea = (r.json().get("data") or {}).get("attributes") or {}
                exceptions.append({"relation": rel, "category": ea.get("category"), "reason": ea.get("reason")})
    return {"found": True, **_lei_brief(rec), "parents": parents, "reporting_exceptions": exceptions}


# ---------------------------------------------------------------------------------------------
# PRH beneficial owner register (X-Road; needs a PRH contract + palveluväylä membership)
# ---------------------------------------------------------------------------------------------
class PRHBeneficialOwnersAdapter:
    """XRoadBeneficialOwnerCompanyRepresentInfoV2 (X-Road v6 SOAP, document/literal) per PRH's published WSDL."""

    source = "PRH"

    def __init__(self):
        self.url = os.environ.get("PRH_BO_API_URL", "").strip()
        self.client = os.environ.get("PRH_BO_CLIENT_ID", "").strip()
        self.user_id = os.environ.get("PRH_BO_USER_ID", "").strip()
        self.service = os.environ.get("PRH_BO_SERVICE", PRH_SERVICE).strip()

    @property
    def configured(self) -> bool:
        return bool(self.url and len(self.client.split("/")) == 4 and self.user_id)

    def envelope(self, business_id: str) -> bytes:
        inst, mclass, mcode, sub = self.client.split("/")
        s = self.service.split("/")
        return f"""<soapenv:Envelope xmlns:soapenv="{NS['soapenv']}" xmlns:xrd="{NS['xrd']}" xmlns:id="{NS['id']}" xmlns:prod="{NS['prod']}" xmlns:esr="{NS['esr']}">
<soapenv:Header>
<xrd:client id:objectType="SUBSYSTEM"><id:xRoadInstance>{inst}</id:xRoadInstance><id:memberClass>{mclass}</id:memberClass><id:memberCode>{mcode}</id:memberCode><id:subsystemCode>{sub}</id:subsystemCode></xrd:client>
<xrd:service id:objectType="SERVICE"><id:xRoadInstance>{s[0]}</id:xRoadInstance><id:memberClass>{s[1]}</id:memberClass><id:memberCode>{s[2]}</id:memberCode><id:subsystemCode>{s[3]}</id:subsystemCode><id:serviceCode>{s[4]}</id:serviceCode><id:serviceVersion>{s[5]}</id:serviceVersion></xrd:service>
<xrd:userId>{self.user_id}</xrd:userId><xrd:id>{uuid.uuid4()}</xrd:id><xrd:protocolVersion>4.0</xrd:protocolVersion>
</soapenv:Header>
<soapenv:Body><prod:XRoadBeneficialOwnerCompanyRepresentInfoV2><prod:request><esr:beneficialOwnerCompanyRepresentInfoV2Request><businessId>{business_id}</businessId><userId>{self.user_id}</userId></esr:beneficialOwnerCompanyRepresentInfoV2Request></prod:request></prod:XRoadBeneficialOwnerCompanyRepresentInfoV2></soapenv:Body>
</soapenv:Envelope>""".encode()

    @staticmethod
    def parse(xml: bytes) -> dict:
        root = ET.fromstring(xml)
        local = lambda el: el.tag.rsplit("}", 1)[-1]  # noqa: E731 - the ESR schema is unqualified below its root
        fault = next((el.text for el in root.iter() if local(el) in ("faultString", "faultstring")), None)
        if fault:
            raise RuntimeError(f"PRH: {fault}")
        owners, none_declared = [], False
        for body in (el for el in root.iter() if local(el) == "body"):
            for ch in body:
                if local(ch) == "noBeneficialOwners" and (ch.text or "").strip() == "true":
                    none_declared = True
                if local(ch) != "naturalPerson":
                    continue
                f = {local(x): x for x in ch}
                txt = lambda k: (f[k].text or "").strip() if k in f and f[k].text else None  # noqa: E731
                bo = {local(x): (x.text or "").strip() for x in f["beneficialOwnership"]} if "beneficialOwnership" in f else {}
                pct = next((float(bo[k]) for k in ("ownershipExtent", "voteExtent") if bo.get(k)), None)
                owners.append({"name": " ".join(x for x in (txt("firstname"), txt("surname")) if x), "kind": "person",
                               "share_pct": pct, "vote_pct": float(bo["voteExtent"]) if bo.get("voteExtent") else None,
                               "other_control": bo.get("otherBeneficialOwnership") == "true",
                               "birth_date": txt("dateOfBirth"), "country": txt("nationality") or txt("homeCountry"),
                               "source": "PRH"})
        return {"owners": owners, "no_beneficial_owners": none_declared}

    async def fetch(self, business_id: str) -> tuple[dict, list[dict]]:
        from services.demo_world.boundary import prh_owners_xml
        xml = await prh_owners_xml(business_id)
        if xml is None and not self.configured:
            return _src("PRH", "not_configured",
                        "PRH beneficial owner register needs a PRH contract, an AML-law purpose and Suomi.fi-palveluväylä "
                        "(X-Road) access: set PRH_BO_API_URL, PRH_BO_CLIENT_ID, PRH_BO_USER_ID"), []
        try:
            if xml is None:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.post(self.url, content=self.envelope(business_id),
                                     headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'})
                r.raise_for_status()
                xml = r.content
            res = self.parse(xml)
        except Exception as exc:  # noqa: BLE001
            return _src("PRH", "error", f"{type(exc).__name__}: {str(exc)[:160]}"), []
        detail = ("Company has declared no beneficial owners" if res["no_beneficial_owners"]
                  else f"{len(res['owners'])} beneficial owner(s) in the trade register")
        return _src("PRH", "ok", detail), res["owners"]


# ---------------------------------------------------------------------------------------------
# UK Companies House PSC register (config switch: COMPANIES_HOUSE_API_KEY)
# ---------------------------------------------------------------------------------------------
_BAND = re.compile(r"ownership-of-shares-(\d+)-to-(\d+)-percent|ownership-of-shares-more-than-(\d+)-percent")


async def companies_house_psc(company_number: str) -> tuple[dict, list[dict]]:
    key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not key:
        return _src("CompaniesHouse", "not_configured", "COMPANIES_HOUSE_API_KEY not set (free key from "
                                                        "developer.company-information.service.gov.uk)"), []
    url = f"{CH_API}/company/{company_number}/persons-with-significant-control"
    try:
        async with httpx.AsyncClient(timeout=25, headers=UA, auth=(key, "")) as c:
            r = await c.get(url, params={"items_per_page": 100})
        if r.status_code == 404:
            return _src("CompaniesHouse", "ok", f"No PSC register for {company_number}", url=url), []
        r.raise_for_status()
        items = r.json().get("items") or []
    except Exception as exc:  # noqa: BLE001
        return _src("CompaniesHouse", "error", f"{type(exc).__name__}: {str(exc)[:160]}", url=url), []
    owners = []
    for it in items:
        if it.get("ceased_on") or it.get("ceased"):
            continue
        bands = [m for n in it.get("natures_of_control") or [] for m in [_BAND.search(n)] if m]
        low = max((int(m.group(1) or m.group(3)) for m in bands), default=None)
        dob = it.get("date_of_birth") or {}
        owners.append({"name": it.get("name"), "kind": "person" if "individual" in (it.get("kind") or "") else "entity",
                       "share_pct": float(low) if low is not None else None,
                       "share_band": next((n for n in it.get("natures_of_control") or [] if "ownership-of-shares" in n), None),
                       "natures_of_control": it.get("natures_of_control"), "country": it.get("country_of_residence")
                       or (it.get("identification") or {}).get("country_registered"),
                       "birth_date": f"{dob['year']}-{dob['month']:02d}" if dob.get("year") and dob.get("month") else None,
                       "source": "CompaniesHouse"})
    return _src("CompaniesHouse", "ok", f"{len(owners)} active PSC(s)", url=url), owners


# ---------------------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------------------
def _company_country(company: dict) -> str:
    return ((company.get("address") or {}).get("country") or company.get("country") or "FI").upper()


async def owners_for_company(company: dict, *, ytj: dict | None = None) -> dict:
    from services.kyc import normalise_business_id, ytj_lookup

    country = _company_country(company)
    bid = normalise_business_id(company.get("business_id")) if country == "FI" else None
    reg_no = bid or company.get("company_number") or company.get("registration_number")
    sources, owners, parents, exceptions, lei = [], [], [], [], None
    # YTJ: reuse the KYC lookup (no ownership data in the open API)
    if bid:
        try:
            y = ytj if ytj and ytj.get("found") is not None else await ytj_lookup(bid)
            sources.append(_src("YTJ", "not_available",
                                (f"{y.get('name')}: {y.get('company_form') or '—'}. " if y.get("found") else "")
                                + "The YTJ open data API v3 has no owners, board members or representatives",
                                url=y.get("url")))
        except Exception as exc:  # noqa: BLE001
            sources.append(_src("YTJ", "error", str(getattr(exc, "detail", exc))[:160]))
    # GLEIF
    try:
        g = await gleif_lookup(reg_no, None if reg_no else company.get("name"), country)
        if g.get("found"):
            lei = {k: g.get(k) for k in ("lei", "name", "url", "status", "registration_status", "registered_as")}
            parents, exceptions = g["parents"], g["reporting_exceptions"]
            sources.append(_src("GLEIF", "ok", f"LEI {g['lei']}: " + (", ".join(f"{p['relation']} parent {p.get('name')} "
                                f"({p.get('lei')})" for p in parents) or "no parent reported; exceptions: " + ", ".join(
                                f"{e['relation']} {e['reason']}" for e in exceptions) or "none"), url=g["url"]))
        else:
            sources.append(_src("GLEIF", "ok", f"No LEI registered for {reg_no or company.get('name')}"))
    except Exception as exc:  # noqa: BLE001
        sources.append(_src("GLEIF", "error", f"{type(exc).__name__}: {str(exc)[:160]}"))
    # PRH beneficial owner register
    if bid:
        s, o = await PRHBeneficialOwnersAdapter().fetch(bid)
        sources.append(s)
        owners += o
    # UK PSC
    if country in ("GB", "UK") and reg_no:
        s, o = await companies_house_psc(str(reg_no))
        sources.append(s)
        owners += o
    declared = [{"name": u.get("name"), "share_pct": u.get("ownership_pct"), "country": u.get("country"),
                 "birth_date": u.get("birth_date"), "pep_declared": bool(u.get("pep")), "control": u.get("control"),
                 "kind": "person", "source": "customer declaration"} for u in company.get("ubos") or [] if u.get("name")]
    return {"sources_checked": sources, "owners": owners, "parents": parents, "lei": lei,
            "reporting_exceptions": exceptions, "declared": declared,
            "none_declared": bool(company.get("ubo_none_declared")), "checked_at": now_iso()}


def compare_declared(result: dict) -> dict:
    """Registry owners vs the customer's declaration (only when a registry source returned owners)."""
    from services.pep import name_score

    reg = [o for o in result.get("owners") or [] if o.get("kind") == "person"]
    if not any(s["status"] == "ok" and s["source"] in ("PRH", "CompaniesHouse") for s in result.get("sources_checked") or []):
        return {"compared": False}
    dec = result.get("declared") or []
    missing = [o["name"] for o in reg if not any(name_score(o["name"], d["name"]) >= 0.9 for d in dec)]
    extra = [d["name"] for d in dec if not any(name_score(o["name"], d["name"]) >= 0.9 for o in reg)]
    return {"compared": True, "missing_from_declaration": missing, "not_in_register": extra}


def _parents_text(result: dict, fi: bool = False) -> str:
    ps = result.get("parents") or []
    if ps:
        rel = {"direct": "välitön emoyhtiö" if fi else "direct parent", "ultimate": "ylin emoyhtiö" if fi else "ultimate parent"}
        return "GLEIF: " + ", ".join(f"{rel[p['relation']]} {p.get('name')} (LEI {p.get('lei')})" for p in ps)
    if result.get("lei"):
        return (f"GLEIF: LEI {result['lei']['lei']}, " + ("emoyhtiötä ei ilmoitettu" if fi else "no parent reported"))
    return ""


def ubo_with_registry(chk: dict, result: dict) -> dict:
    """The KYC `ubo` check (customer declaration) with the registry sources added; a mismatch with PRH / Companies House
    owners turns it into warn."""
    cmp = compare_declared(result)
    extra_en, extra_fi = [], []
    if _parents_text(result):
        extra_en.append(_parents_text(result))
        extra_fi.append(_parents_text(result, fi=True))
    for s in result.get("sources_checked") or []:
        if s["source"] in ("PRH", "CompaniesHouse") and s["status"] == "ok":
            extra_en.append(f"{s['source']}: {s['detail']}")
            extra_fi.append(f"{s['source']}: {len([o for o in result['owners'] if o['source'] == s['source']])} edunsaajaa rekisterissä")
    status = chk["status"]
    if cmp.get("compared") and (cmp["missing_from_declaration"] or cmp["not_in_register"]):
        status = "warn"
        extra_en.append("declaration differs from the register: missing " + (", ".join(cmp["missing_from_declaration"]) or "—")
                        + "; not in the register " + (", ".join(cmp["not_in_register"]) or "—"))
        extra_fi.append("ilmoitus poikkeaa rekisteristä: puuttuu " + (", ".join(cmp["missing_from_declaration"]) or "—")
                        + "; ei rekisterissä " + (", ".join(cmp["not_in_register"]) or "—"))
    srcs = ", ".join(f"{s['source']} {s['status']}" for s in result.get("sources_checked") or [])
    return {**chk, "status": status, "summary": "; ".join([chk["summary"]] + extra_en),
            "registry_fi": "; ".join(extra_fi), "comparison": cmp,
            "registry": {k: result.get(k) for k in ("sources_checked", "owners", "parents", "lei", "reporting_exceptions")},
            "source": f"{chk['source']}; registries: {srcs}"}
