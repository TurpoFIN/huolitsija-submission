"""Beneficial owners and PEP checks against the REAL sources (no mocks): Wikidata, GLEIF, YTJ, OpenSanctions and
Companies House (401 probes without a licence / key), and the KYC wiring.

Direct import on its own database (huo_s12_owners_test). The HTTP tests at the end run against a local API
(S12_API, default http://localhost:8124/api, DB S12_DB huo_s12_owners_http) and skip when it is not running.

Live facts these tests rely on (checked 2026-10-24): Petteri Orpo is Prime Minister of Finland (Q6029546, since
2023-06-20); Alexander Stubb is President of Finland (Q503143, since 2024-03-01); Sanna Marin (Q16986436) left office
2023-06-20 and the Eduskunta 2023-09-12; Antti Lindtman (Q11852155) is a member of the Eduskunta; Suzanne Innes-Stubb
(Q17278098) is the President's spouse; Onninen Oy (1071207-9) is consolidated by Kesko Oyj (LEI 743700OX6HSVMCAHPB95).
"""
import asyncio
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if "core.db" not in sys.modules:
    os.environ["DB_NAME"] = "huo_s12_owners_test"

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core.audit import agent_actor  # noqa: E402
from core.db import db  # noqa: E402
from services import beneficial_owners as bo  # noqa: E402
from services import kyc, pep  # noqa: E402

KESKO_LEI = "743700OX6HSVMCAHPB95"
WSDL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "compliance", "prh",
                    "XRoadBeneficialOwnerCompanyRepresentInfoV2.wsdl")


def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for k in ("OPENSANCTIONS_API_KEY", "COMPANIES_HOUSE_API_KEY", "PRH_BO_API_URL", "PRH_BO_CLIENT_ID", "PRH_BO_USER_ID"):
        monkeypatch.delenv(k, raising=False)


def _match(res, qid):
    m = next((m for m in res["matches"] if m.get("qid") == qid), None)
    assert m, (qid, [x.get("qid") for x in res["matches"]])
    return m


def _pos(m, qid):
    return [p for p in m["positions"] if p["qid"] == qid]


# ---------------------------------------------------------------------------------------------
# AMLD art. 22 phases (pure rule)
# ---------------------------------------------------------------------------------------------
def test_phase_rule_12_months():
    today = date(2026, 10, 24)
    assert pep.phase(None, today) == "current"
    assert pep.phase(date(2027, 1, 1), today) == "current"
    assert pep.phase(date(2025, 10, 25), today) == "former_within_12m"
    assert pep.phase(today - timedelta(days=365), today) == "former_within_12m"
    assert pep.phase(today - timedelta(days=366), today) == "former_over_12m"
    assert pep.phase(None, today, undated=True) == "undated"


# ---------------------------------------------------------------------------------------------
# Wikidata (live)
# ---------------------------------------------------------------------------------------------
def test_orpo_current_prime_minister_is_pep():
    r = run(pep.check_person("Petteri Orpo", country="FI", refresh=True))
    assert r["status"] == "pep", r
    m = _match(r, "Q6029546")
    assert m["pep"] and m["pep_phase"] == "current" and m["url"] == "https://www.wikidata.org/wiki/Q6029546"
    pm = _pos(m, "Q738695")  # Prime Minister of Finland
    assert pm and pm[0]["start"] == "2023-06-20" and pm[0]["end"] is None and pm[0]["phase"] == "current"
    assert "head_of_state_or_government" in pm[0]["categories"] and pm[0]["label"] == "Prime Minister of Finland"
    assert pm[0]["url"] == "https://www.wikidata.org/wiki/Q738695"
    assert r["sources"][0]["source"] == "Wikidata" and r["sources"][0]["status"] == "ok"
    assert {"source": "OpenSanctions", "status": "not_configured"}.items() <= r["sources"][-1].items()


def test_stubb_president_is_pep():
    r = run(pep.check_person("Alexander Stubb", country="FI", refresh=True))
    assert r["status"] == "pep"
    m = _match(r, "Q503143")
    pres = [p for p in m["positions"] if p["label"] == "President of Finland"]
    assert pres and pres[0]["start"] == "2024-03-01" and pres[0]["phase"] == "current"
    assert "head_of_state_or_government" in pres[0]["categories"]


def test_marin_former_pm_over_12_months_stays_pep_for_a_person_to_decide():
    r = run(pep.check_person("Sanna Marin", country="FI", refresh=True))
    m = _match(r, "Q16986436")
    pm = _pos(m, "Q738695")
    assert pm and pm[0]["start"] == "2019-12-10" and pm[0]["end"] == "2023-06-20"
    # every prominent function ended more than 12 months ago (MP 2023-09-12 is the latest)
    assert all(p["phase"] == "former_over_12m" for p in m["positions"]), [(p["label"], p["end"]) for p in m["positions"]]
    latest = max(p["end"] for p in m["positions"])
    assert date.fromisoformat(latest) + timedelta(days=365) < datetime.now(timezone.utc).date()
    # AMLD art. 22: "at least 12 months ... until deemed to pose no further risk" -> still pep, phase tells why
    assert r["status"] == "pep" and m["pep_phase"] == "former_over_12m"


def test_finnish_mp_is_pep():
    r = run(pep.check_person("Antti Lindtman", country="FI", refresh=True))
    assert r["status"] == "pep"
    m = _match(r, "Q11852155")
    mp = [p for p in _pos(m, "Q17592486") if p["phase"] == "current"]  # member of the Parliament of Finland
    assert mp and mp[0]["start"] >= "2023-04-01" and "parliament" in mp[0]["categories"]


def test_spouse_of_president_is_rca():
    r = run(pep.check_person("Suzanne Innes-Stubb", country="FI", refresh=True))
    assert r["status"] == "rca", r
    m = _match(r, "Q17278098")
    assert not m["pep"] and m["rca"]
    rel = next(x for x in m["relatives"] if x["qid"] == "Q503143")
    assert rel["relation"] == "spouse" and rel["property"] == "P26" and rel["pep_phase"] == "current"


def test_non_pep_names_are_clear():
    r = run(pep.check_person("Teemu Pukki", country="FI", refresh=True))  # a footballer: human found, no office
    assert r["status"] == "clear" and r["matches"] == []
    assert "Q366753" in r["sources"][0]["detail"]
    r2 = run(pep.check_person(f"Qorvath Ilmendrax {uuid.uuid4().hex[:6]}", country="FI", refresh=True))
    assert r2["status"] == "clear" and r2["sources"][0]["detail"] == "no item with this name"


def test_birth_year_filters_namesakes():
    r = run(pep.check_person("Petteri Orpo", country="FI", birth_date="1950", refresh=True))
    assert r["status"] == "clear"  # Orpo was born 1969


def test_cache_30_days():
    first = run(pep.check_person("Petteri Orpo", country="FI"))
    again = run(pep.check_person("petteri  ORPO", country="Finland"))
    assert again["cached"] is True and again["key"] == first["key"] and again["status"] == "pep"
    doc = run(db.pep_checks.find_one({"key": first["key"]}))
    assert doc["source"] == "Wikidata" and doc["matches"][0]["qid"] == "Q6029546"


# ---------------------------------------------------------------------------------------------
# OpenSanctions / Companies House / PRH: config switches (real endpoints, no licence bought)
# ---------------------------------------------------------------------------------------------
def test_opensanctions_switch_uses_the_real_api(monkeypatch):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "not-a-real-key")
    r = run(pep.check_person("Petteri Orpo", country="FI", refresh=True))
    assert r["source"] == "OpenSanctions" and r["status"] == "error"
    assert "HTTP 401" in r["sources"][0]["detail"], r["sources"]


def test_companies_house_switch(monkeypatch):
    s, owners = run(bo.companies_house_psc("00445790"))
    assert s["status"] == "not_configured" and owners == []
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "not-a-real-key")
    s, owners = run(bo.companies_house_psc("00445790"))
    assert s["status"] == "error" and "401" in s["detail"] and owners == []


def test_prh_adapter_matches_published_wsdl_and_reports_not_configured(monkeypatch):
    a = bo.PRHBeneficialOwnersAdapter()
    s, owners = run(a.fetch("1071207-9"))
    assert s["status"] == "not_configured" and owners == []
    monkeypatch.setenv("PRH_BO_API_URL", "http://127.0.0.1:9/xroad")  # discard port: no security server here
    monkeypatch.setenv("PRH_BO_CLIENT_ID", "FI-TEST/COM/0000000-0/huolitsija")
    monkeypatch.setenv("PRH_BO_USER_ID", "huolitsija-test")
    a = bo.PRHBeneficialOwnersAdapter()
    env = ET.fromstring(a.envelope("1071207-9"))
    w = ET.parse(WSDL).getroot()
    assert w.get("targetNamespace") == "http://xroad.virre.prh.fi/producer"
    ops = {o.get("name") for o in w.iter("{http://schemas.xmlsoap.org/wsdl/}operation")}
    assert "XRoadBeneficialOwnerCompanyRepresentInfoV2" in ops
    body = env.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    op = body[0]
    assert op.tag == "{http://xroad.virre.prh.fi/producer}XRoadBeneficialOwnerCompanyRepresentInfoV2"
    req = op[0][0]
    assert req.tag == "{http://www.prh.fi/ESRServices}beneficialOwnerCompanyRepresentInfoV2Request"
    assert [c.tag for c in req] == ["businessId", "userId"] and req[0].text == "1071207-9"
    hdr = {c.tag.rsplit("}", 1)[-1] for c in env.find("{http://schemas.xmlsoap.org/soap/envelope/}Header")}
    parts = {p.get("name") for m in w.iter("{http://schemas.xmlsoap.org/wsdl/}message") if m.get("name") == "requestheader"
             for p in m}
    assert hdr == parts - {"issue"}  # X-Road headers per the WSDL; `issue` is optional and not sent
    s, owners = run(a.fetch("1071207-9"))
    assert s["status"] == "error" and owners == []  # never invented owners


# ---------------------------------------------------------------------------------------------
# GLEIF (live)
# ---------------------------------------------------------------------------------------------
def test_gleif_subsidiary_has_kesko_as_ultimate_parent():
    r = run(bo.owners_for_company({"business_id": "1071207-9", "name": "Onninen Oy", "address": {"country": "FI"}}))
    assert r["lei"]["lei"] == "743700HUNZJ7XDH11G65"
    by = {p["relation"]: p for p in r["parents"]}
    assert by["ultimate"]["lei"] == KESKO_LEI and by["ultimate"]["name"] == "Kesko Oyj"
    assert by["ultimate"]["relationship_type"] == "IS_ULTIMATELY_CONSOLIDATED_BY" and by["direct"]["lei"] == KESKO_LEI
    src = {s["source"]: s for s in r["sources_checked"]}
    assert src["GLEIF"]["status"] == "ok" and src["PRH"]["status"] == "not_configured"
    assert src["YTJ"]["status"] in ("not_available", "error") and r["owners"] == []


def test_gleif_parent_reports_exception():
    g = run(bo.gleif_lookup("0109862-8"))
    assert g["found"] and g["lei"] == KESKO_LEI and g["parents"] == []
    assert {(e["relation"], e["reason"]) for e in g["reporting_exceptions"]} == {("direct", "NO_KNOWN_PERSON"),
                                                                                 ("ultimate", "NO_KNOWN_PERSON")}


# ---------------------------------------------------------------------------------------------
# KYC wiring (direct import, real YTJ / VIES / EORI / GLEIF / Wikidata)
# ---------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pep_company():
    cid = f"s12-owners-{uuid.uuid4().hex[:8]}"
    run(db.companies.insert_one({"id": cid, "name": "Onninen Oy", "business_id": "1071207-9", "status": "active",
                                 "address": {"country": "FI"}, "test": True, "kyc_status": "not_started",
                                 "ubos": [{"id": "u1", "name": "Petteri Orpo", "country": "FI", "ownership_pct": 30.0,
                                           "pep": False}], "signatory": {"name": "Teemu Pukki", "title": "CEO"}}))
    return cid


def test_kyc_with_declared_pep_owner_needs_a_person(pep_company):
    c = run(kyc.run_kyc(pep_company, None))
    chk = c["kyc"]["pep"]
    assert chk["status"] == "review" and chk["legal_point"] == "pep_relationship"
    assert "Petteri Orpo" in chk["summary"] and "Prime Minister of Finland" in chk["summary"] and "Q6029546" in chk["summary"]
    assert "PEP" in chk["summary_fi"] and chk["source"].startswith("Wikidata")
    assert {p["name"]: p["status"] for p in chk["persons"]} == {"Petteri Orpo": "pep", "Teemu Pukki": "clear"}
    assert c["kyc"]["risk"]["pep"] == "review" and c["kyc_status"] == "in_review"
    assert "Kesko Oyj" in c["kyc"]["ubo"]["summary"] and "ylin emoyhtiö Kesko Oyj" in c["kyc"]["ubo"]["summary_fi"]
    card = run(db.approvals.find_one({"kind": "kyc_review", "ref_id": pep_company, "status": "pending"}, {"_id": 0}))
    assert card and card["legal_point"] == "pep_relationship" and card["data"]["pep"]["hit_keys"]
    assert card["agent_decides"] is False and card["title_fi"].startswith("PEP-tarkistus")
    # an agent never approves a PEP relationship
    with pytest.raises(HTTPException) as e:
        run(kyc.decide_kyc(pep_company, "approved", agent_actor("kyc"), "agent tries"))
    assert e.value.status_code == 403
    person = {"id": "u-compliance", "name": "Compliance Officer", "type": "user", "org_type": "internal"}
    c = run(kyc.decide_kyc(pep_company, "approved", person, "PEP relationship approved by senior management"))
    assert c["kyc_status"] == "approved" and c["kyc"]["pep"]["status"] == "pass"
    assert set(chk["hit_keys"]) <= set(c["kyc"]["pep_decision"]["approved"])
    # a re-run keeps the approved PEP relationship as pass (same matches), with the approver named
    c = run(kyc.run_kyc(pep_company, None))
    assert c["kyc"]["pep"]["status"] == "pass" and c["kyc"]["pep"].get("approved") is True
    assert "Compliance Officer" in c["kyc"]["pep"]["summary"]


def test_kyc_without_persons_is_not_applicable():
    cid = f"s12-owners-{uuid.uuid4().hex[:8]}"
    run(db.companies.insert_one({"id": cid, "name": "Kesko Oyj", "business_id": "0109862-8", "status": "active",
                                 "test": True, "ubo_none_declared": True}))
    c = run(kyc.run_kyc(cid, None))
    assert c["kyc"]["pep"]["status"] == "not_applicable"
    assert "GLEIF: LEI 743700OX6HSVMCAHPB95, no parent reported" in c["kyc"]["ubo"]["summary"]


def test_weekly_refresh_reruns_kyc_only_for_new_hits(pep_company):
    from routes.compliance_owners import pep_refresh

    cid = f"s12-owners-{uuid.uuid4().hex[:8]}"
    run(db.companies.insert_one({"id": cid, "name": "Kesko Oyj", "business_id": "0109862-8", "status": "active",
                                 "test": True, "kyc_status": "in_review", "kyc": {},
                                 "ubos": [{"id": "u1", "name": "Antti Lindtman", "country": "FI", "pep": False}]}))
    res = run(pep_refresh({"kind": "compliance.pep_refresh", "payload": {"force": True}}))
    assert not res["halted"] and res["companies"] >= 2
    rerun = {x["company_id"]: x for x in res["kyc_rerun"]}
    assert cid in rerun and pep_company not in rerun  # the approved PEP relationship is not reopened
    c = run(db.companies.find_one({"id": cid}, {"_id": 0}))
    assert c["kyc"]["pep"]["status"] == "review" and "Antti Lindtman" in c["kyc"]["pep"]["summary"]
    assert run(db.approvals.find_one({"ref_id": cid, "kind": "kyc_review", "legal_point": "pep_relationship"}))
    assert run(db.audit_log.find_one({"action": "pep_refresh", "actor.id": "compliance"}))
    assert run(pep_refresh({"kind": "compliance.pep_refresh", "payload": {}}))["skipped"] == "weekly"


# ---------------------------------------------------------------------------------------------
# HTTP (local API)
# ---------------------------------------------------------------------------------------------
API = os.environ.get("S12_API", "http://localhost:8124/api")
PASSWORD = os.environ.get("S12_DEMO_PASSWORD", "s12-local-Demo-2026")


def _client(email):
    c = httpx.Client(base_url=API, timeout=180)
    try:
        c.get("/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local API not reachable at {API}: {exc}")
    r = c.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return c


def test_http_pep_check_and_owners():
    ops = _client("ops.demo@huolitsija.fi")
    r = ops.post("/compliance/pep-check", json={"name": "Alexander Stubb", "country": "FI"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pep" and r.json()["matches"][0]["qid"] == "Q503143"
    cust = _client("customer.demo@huolitsija.fi")
    assert cust.post("/compliance/pep-check", json={"name": "Alexander Stubb"}).status_code == 403
    company = cust.get("/company").json()
    r = ops.post(f"/compliance/companies/{company['id']}/owners/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {s["source"] for s in body["sources_checked"]} >= {"GLEIF", "PRH"}
    assert ops.get(f"/compliance/companies/{company['id']}/owners").json()["checked_at"] == body["checked_at"]
    assert cust.get(f"/compliance/companies/{company['id']}/owners").status_code == 403
    assert body["persons"] is not None  # ops see each person's PEP result ...
    own = cust.get("/company").json()   # ... the customer does not
    assert "persons" not in (own.get("beneficial_owners") or {}) and "persons" not in ((own.get("kyc") or {}).get("pep") or {})
