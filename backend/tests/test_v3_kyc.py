"""KYC and sanctions screening through the real API against the live registers: PRH/YTJ, EU VIES, EU EOS (EORI)
and the EU, UN and OFAC sanctions lists. The company is the real Helios One Oy (3596623-5). No mocks.

Each run signs up a fresh customer (kyc.test+<uuid>@huolitsija.fi) in the server's database.
Run: ~/work/venv/bin/python -m pytest -q /home/user/work/huolitsija/backend/tests/test_v3_kyc.py
"""
import uuid

import pytest

import test_v3_support as S


@pytest.fixture(scope="module")
def ops():
    c = S.client(S.OPS)
    lists = c.get("/screening/lists").json()["items"]
    if not all(x["loaded"] for x in lists):
        c.post("/screening/refresh")
        lists = S.wait(lambda: (lambda xs: xs if all(x["loaded"] for x in xs) else None)(c.get("/screening/lists").json()["items"]),
                       timeout=300, every=5)
        assert lists, "sanctions lists did not load within 5 minutes"
    return c


def _release_business_id(bid: str, keep: str | None = None):
    """Earlier test runs leave a test company holding the Business ID (one company per Business ID is enforced).
    Release it only when every user of that company is a kyc.test+ account, i.e. it is test data."""
    db = S.mongo()
    for comp in db.companies.find({"business_id": bid, "id": {"$ne": keep}}, {"_id": 0, "id": 1}):
        emails = [u["email"] for u in db.users.find({"company_id": comp["id"]}, {"email": 1})]
        if emails and all(e.startswith("kyc.test+") for e in emails):
            db.companies.update_one({"id": comp["id"]}, {"$set": {"business_id": None, "status": "archived", "test": True,
                                                                  "archived_reason": "V3 KYC test released the Business ID"}})
        else:
            pytest.skip(f"Business ID {bid} belongs to a non-test account ({emails}); not touching it")


@pytest.fixture(scope="module")
def helios(ops):
    _release_business_id("3596623-5")
    c = S.client()
    email = f"kyc.test+{uuid.uuid4().hex[:10]}@huolitsija.fi"
    r = c.post("/auth/signup", json={"email": email, "password": "Kyc-test-2026-pw", "name": "KYC Test",
                                     "company_name": "Helios One Oy", "locale": "en", "accept_terms": True})
    assert r.status_code == 200, r.text
    S.mark_test_company(email)
    look = c.post("/company/lookup", json={"business_id": "3596623-5"}).json()
    r = c.patch("/company", json={"business_id": "3596623-5", "address": {k: look["address"].get(k) for k in ("street", "postal_code", "city", "country")}})
    assert r.status_code == 200, r.text
    info = {"client": c, "company_id": r.json()["id"], "email": email}
    yield info
    _release_business_id("3596623-5")


def checklist(view):
    return {x["key"]: x for x in view["kyc_checklist"]}


def run_kyc(c):
    r = c.post("/company/kyc/run")
    assert r.status_code == 200, r.text
    return r.json()


def test_kyc_live_registers_without_ubo_goes_to_review(helios, ops):
    view = run_kyc(helios["client"])
    ck = checklist(view)
    assert ck["ytunnus"]["status"] == "pass"
    assert ck["ytj"]["status"] == "pass", ck["ytj"]           # registered, valid, name matches YTJ
    assert ck["vies"]["status"] == "pass", ck["vies"]         # FI35966235 active in VIES
    assert ck["eori"]["status"] == "warn", ck["eori"]         # no EORI registered yet: readiness, not risk
    assert "EORI Number Registration Service" in ck["eori"]["summary"]
    assert ck["sanctions"]["status"] == "pass", ck["sanctions"]
    assert {"Helios One Oy", "Helios One GmbH"} <= set(ck["sanctions"]["screened_names"])  # auxiliary names from YTJ too
    assert {x["list"] for x in ck["sanctions"]["lists"]} == {"eu", "un", "ofac", "uk", "csl"}  # s12 added UK + US CSL
    assert ck["ubo"]["status"] == "warn"                       # not declared
    assert view["kyc_status"] == "in_review"
    assert view["vat_registered"] is True and view["vat_id"] == "FI35966235"
    assert view["ytj"]["name"] == "Helios One Oy" and view["ytj"]["fetched_at"]
    # the review is a real approval for someone with kyc.approve
    appr = ops.get("/approvals", params={"status": "pending", "kind": "kyc_review"}).json()
    items = appr.get("items", appr) if isinstance(appr, dict) else appr
    assert any(a["ref_id"] == helios["company_id"] for a in items)


def test_kyc_green_with_declared_owner_is_auto_approved(helios, ops):
    """One decision gate (core.agent_runtime.decide): a green run is approved by the KYC agent while Autopilot is on;
    with Autopilot off the same run is a kyc_review card in the owner queue."""
    c, cid = helios["client"], helios["company_id"]
    r = c.patch("/company", json={"ubos": [{"name": "Test Owner (test data)", "birth_date": "1980-01-01", "country": "FI",
                                            "ownership_pct": 100, "pep": False}],
                                  "signatory": {"name": "KYC Test", "title": "Board member"}})
    assert r.status_code == 200, r.text
    autopilot = lambda on: ops.post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable"})  # noqa: E731
    assert autopilot(False).status_code == 200
    view = run_kyc(c)
    ck = checklist(view)
    assert ck["ubo"]["status"] == "pass" and ck["sanctions"]["status"] == "pass"
    assert view["kyc_status"] == "in_review" and view["kyc"]["green"] is True, {k: v["status"] for k, v in ck.items()}
    cards = [a for a in ops.get("/approvals", params={"status": "pending", "kind": "kyc_review"}).json()["items"] if a["ref_id"] == cid]
    assert len(cards) == 1 and [x["code"] for x in cards[0]["reasons"]] == ["autopilot_off"], cards
    try:
        assert autopilot(True).status_code == 200
        view = run_kyc(c)
        assert view["kyc_status"] == "approved", {k: v["status"] for k, v in checklist(view).items()}
        assert view["kyc"]["decided_by"]["type"] == "agent" and view["kyc"]["agent_decision_id"]
        # the parked card is closed with the approval
        card = ops.get(f"/approvals/{cards[0]['id']}").json()
        assert card["status"] == "cancelled", card["status"]
    finally:
        autopilot(False)


def test_sanctioned_owner_blocks_auto_approval_and_needs_a_sanctions_decision(helios, ops):
    c = helios["client"]
    r = c.patch("/company", json={"ubos": [{"name": "Arkady Romanovich Rotenberg", "country": "RU", "ownership_pct": 30}]})
    assert r.status_code == 200
    view = run_kyc(c)
    ck = checklist(view)
    staff_ck = checklist(ops.get(f"/ops/customers/{helios['company_id']}").json())
    assert staff_ck["sanctions"]["status"] == "fail", staff_ck["sanctions"]
    hit = staff_ck["sanctions"]["hits"][0]
    assert hit["role"] == "beneficial owner" and hit["score"] >= 0.86 and hit["list"] in ("eu", "un", "ofac") and hit["explanation"]
    assert view["kyc_status"] == "in_review"
    # no tipping-off: the customer sees "in review", never the hit, the list entry or the score
    assert view["sanctions_status"] == "in_review" and ck["sanctions"]["status"] == "in_review"
    raw = view["kyc"]["sanctions"]
    assert not ck["sanctions"].get("hits") and "hits" not in raw and "cleared_hits" not in view["kyc"]
    assert set(raw) <= {"status", "checked_at", "source", "lists", "screened_names", "summary", "summary_fi"}
    cid = helios["company_id"]
    staff = ops.get(f"/ops/customers/{cid}").json()
    sck = {x["key"]: x for x in staff["kyc_checklist"]}["sanctions"]
    assert staff["sanctions_status"] == "potential_match" and sck["status"] == "fail"
    # approving without a sanctions decision is refused (RULE K-1); approving + blocked is contradictory
    r = ops.post(f"/ops/customers/{cid}/kyc-decision", json={"decision": "approved", "note": "looks fine"})
    assert r.status_code == 400 and "K-1" in r.json()["detail"]
    r = ops.post(f"/ops/customers/{cid}/kyc-decision", json={"decision": "approved", "note": "x", "sanctions_decision": "blocked"})
    assert r.status_code == 400
    # a customer cannot decide their own KYC
    assert c.post(f"/ops/customers/{cid}/kyc-decision", json={"decision": "approved", "note": "me"}).status_code == 403
    # compliance clears it as a false positive (test data) with a note
    r = ops.post(f"/ops/customers/{cid}/kyc-decision", json={"decision": "approved", "note": "Test data: not the listed person",
                                                             "sanctions_decision": "clear"})
    assert r.status_code == 200, r.text
    assert r.json()["kyc_status"] == "approved" and r.json()["sanctions_status"] == "clear"
    # re-running does not re-raise the same cleared match ...
    view = run_kyc(c)
    assert checklist(view)["sanctions"]["status"] == "pass" and "cleared earlier" in checklist(view)["sanctions"]["summary"]
    # ... but a new listed name is screened afresh
    c.patch("/company", json={"ubos": [{"name": "Arkady Romanovich Rotenberg", "country": "RU", "ownership_pct": 30},
                                       {"name": "Gennady Nikolayevich Timchenko", "country": "RU", "ownership_pct": 30}]})
    view = run_kyc(c)
    assert checklist(view)["sanctions"]["status"] == "in_review"
    assert view["kyc_status"] == "in_review"


def test_ops_customer_views_and_scoping(helios, ops):
    cid = helios["company_id"]
    lst = ops.get("/ops/customers", params={"q": "Helios", "include_test": 1}).json()
    assert any(x["id"] == cid for x in lst["items"]) and "counts" in lst
    assert all(x["id"] != cid for x in ops.get("/ops/customers", params={"q": "Helios"}, headers={"X-Include-Test-Tenants": "false"}).json()["items"])  # test data hidden
    d = ops.get(f"/ops/customers/{cid}").json()
    assert d["business_id"] == "3596623-5" and d["audit"] and d["screening_lists"]
    assert helios["client"].get("/ops/customers").status_code == 403
    assert helios["client"].get(f"/ops/customers/{cid}").status_code == 403


@pytest.mark.parametrize("name", ["Rosoboronexport", "Bank Rossiya", "ROSOBORONEXPORT JSC", "Joint-Stock Company Rosoboronexport"])
def test_screening_api_known_sanctioned_entities(ops, name):
    j = ops.post("/screening/check", json={"name": name, "kind": "entity"}).json()
    assert j["status"] == "potential_match", j
    assert j["hits"][0]["score"] >= 0.86 and j["hits"][0]["explanation"] and j["complete"]


@pytest.mark.parametrize("name", ["Pohjolan Kaluste Oy", "Helios One Oy", "Kesko Oyj", "Valio Oy", "Wärtsilä Oyj Abp",
                                  "Matti Meikäläinen", "Shenzhen Huaxin Microelectronics Co., Ltd."])
def test_screening_api_normal_names_are_clear(ops, name):
    j = ops.post("/screening/check", json={"name": name, "kind": "entity"}).json()
    assert j["status"] == "clear", [(h["matched_name"], h["score"]) for h in j.get("hits", [])]


@pytest.mark.parametrize("name", ["", "   ", "x" * 2000])
def test_screening_api_rejects_garbage(ops, name):
    r = ops.post("/screening/check", json={"name": name, "kind": "entity"})
    assert r.status_code in (400, 422)


def test_screening_api_is_internal_only(helios):
    assert helios["client"].post("/screening/check", json={"name": "Bank Rossiya"}).status_code == 403


def test_finnish_summaries_and_poa_rules(helios, ops):
    c = helios["client"]
    view = c.get("/company").json()
    ck = checklist(view)
    assert "tarkistusnumero" in ck["ytunnus"]["summary_fi"] and ck["vies"]["summary_fi"]
    t = c.get("/company/poa/terms", params={"lang": "fi"}).json()
    assert t["lang"] == "fi" and "Suora edustus" in t["types"]["direct"]["text"] and t["indirect_allowed"] is False
    body = {"type": "indirect", "scope": ["import"], "valid_from": "2026-11-01", "valid_to": "2027-10-31",
            "typed_name": "KYC Test", "accept": True}
    r = c.post("/company/poa", json=body)
    assert r.status_code == 403 and "Indirect" in r.json()["detail"]                     # RULE R-1
    r = c.post("/company/poa", json={**body, "type": "direct"})
    assert r.status_code == 200, r.text
    poa = r.json()["poa"]
    assert poa["signature"]["typed_name"] == "KYC Test" and poa["signature"]["ip"] and poa["document_id"]
    pdf = c.get(f"/documents/{poa['document_id']}/file")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_manual_eori_verification_is_audited_and_bound_to_the_number(ops):
    from core.refs import ytunnus_valid

    comp = S.ensure_test_customer()
    cid = comp["id"]
    # an EORI no register holds: the live EU EOS does not know it, and the sandbox register (this is a test tenant with a
    # reserved 399xxxx Business ID) holds only FI<own Business ID>
    body = comp["business_id"][:7]
    other = next(f"{n:07d}-{d}" for n in range(int(body) + 1, 3999999) for d in range(10) if ytunnus_valid(f"{n:07d}-{d}"))
    unknown = f"FI{other}"
    S.mongo().companies.update_one({"id": cid}, {"$set": {"eori": unknown}, "$unset": {"kyc.eori": ""}})
    r = ops.post(f"/ops/customers/{cid}/kyc/run")
    assert r.status_code == 200, r.text
    eo = checklist(r.json())["eori"]
    assert eo["status"] == "fail", eo                               # neither live EOS nor the sandbox register knows it
    cust = S.client(S.TEST_CUSTOMER)
    assert cust.post(f"/ops/customers/{cid}/eori-verification", json={"status": "verified_manually", "note": "x" * 10}).status_code == 403
    assert ops.post(f"/ops/customers/{cid}/eori-verification", json={"status": "verified_manually", "note": ""}).status_code == 422
    r = ops.post(f"/ops/customers/{cid}/eori-verification", json={"status": "verified_manually", "note": "Test company; synthetic EORI"})
    assert r.status_code == 200, r.text
    eo = checklist(r.json())["eori"]
    assert eo["status"] == "manual" and eo["manual"]["note"] == "Test company; synthetic EORI" and eo["registry_result"]["status"] == "fail"
    assert any(a["action"] == "eori_verified_manually" for a in r.json()["audit"])
    # a KYC re-run keeps the documented verification while the registry still says no
    eo = checklist(ops.post(f"/ops/customers/{cid}/kyc/run").json())["eori"]
    assert eo["status"] == "manual"
    # E-1 on a declaration passes with the advisory text
    from services.kyc import manual_eori_applies

    k = S.mongo().companies.find_one({"id": cid})["kyc"]["eori"]
    assert manual_eori_applies(k, unknown) and not manual_eori_applies(k, "FI0109862-8")
    # revoke restores the registry result
    r = ops.post(f"/ops/customers/{cid}/eori-verification", json={"status": "revoked", "note": "Test cleanup"})
    assert checklist(r.json())["eori"]["status"] == "fail"
    S.mongo().companies.update_one({"id": cid}, {"$set": {"eori": comp["eori"]}, "$unset": {"kyc.eori": ""}})
    S.ensure_test_customer()  # back to the fixture state for the customs tests
