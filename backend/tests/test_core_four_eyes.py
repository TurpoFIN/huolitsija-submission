"""Four-eyes on customs filing and claim payment (s4 D6), through the real approvals API.

With Autopilot off, the person who requested a customs_filing approval or a claim payment cannot approve it. When no
other active internal user holds the kind's permission, the requester may approve with `override: true` and a reason of
at least 10 characters; the override is stored on the approval and audited. Rejecting your own request stays allowed.
A colleague is counted by permission (customs.file / claims.approve), so a finance user unblocks claims, not filings."""
import uuid

import httpx
import pytest

from test_v4_support import API, client_for, customer, customer_company_id, db, iso, make_shipment, now, ops


def _me(client) -> dict:
    return client.get("/auth/me").json()["user"]


def _filing_approval(requester: dict) -> dict:
    """A pending customs_filing approval requested by `requester` (no declaration behind it: deciding it files nothing)."""
    a = {"id": str(uuid.uuid4()), "kind": "customs_filing", "ref_type": "customs_declaration",
         "ref_id": f"four-eyes-{uuid.uuid4().hex[:8]}", "title": "File import declaration LRN-TEST (four-eyes)",
         "summary": "", "reason": "", "company_id": customer_company_id(), "amount_cents": 1000, "status": "pending",
         "requested_by": {"type": "user", "id": requester["id"], "name": requester.get("name"), "org_type": "internal"},
         "data": {}, "link": None, "created_at": iso(now()), "decided_at": None, "decided_by": None, "decision_note": None}
    db.approvals.insert_one(dict(a))
    return a


def _claim_approval(decision: str = "approve") -> str:
    """A real claim proposal by ops (the requester) -> its pending approval id."""
    s = make_shipment({"id": customer_company_id(), "name": "Pohjolan Kaluste Oy"}, mode="road", regime="CMR",
                      delivered_days_ago=2, origin=("DEHAM", "Hamburg", "DE", 53.55, 9.99))
    r = customer().post("/claims", json={"shipment_id": s["id"], "type": "damage", "submit": True,
                                         "description": "Kaksi lavaa kaatui kuljetuksessa (four-eyes testi).",
                                         "amount_claimed_cents": 90000, "gross_kg_affected": 200, "packages_affected": 2})
    assert r.status_code == 200, r.text
    cl = r.json()
    assert ops().post(f"/claims/{cl['id']}/review").status_code == 200
    body = {"decision": decision, "note": "Vaurio todettu rahtikirjasta (testi)"}
    if decision == "approve":
        body["amount_cents"] = 60000
    p = ops().post(f"/claims/{cl['id']}/propose", json=body)
    assert p.status_code == 200, p.text
    return p.json()["approval_id"]


def _decide(client, approval_id: str, **body) -> httpx.Response:
    return client.post(f"/approvals/{approval_id}/decide", json={"decision": "approved", **body})


def _listed(approval_id: str) -> dict:
    return next(a for a in ops().get("/approvals?status=pending").json()["items"] if a["id"] == approval_id)


@pytest.fixture
def colleague():
    """A second internal user, invited and accepted through the team API; disabled again afterwards."""
    def make(role: str):
        email = f"four-eyes-{uuid.uuid4().hex[:8]}@example.fi"
        r = ops().post("/ops/users/invite", json={"email": email, "role": role, "locale": "fi"})
        assert r.status_code == 200, r.text
        token = db.tokens.find_one({"kind": "invite", "data.email": email})["token"]
        pw = "Colleague-" + uuid.uuid4().hex[:10]
        r = httpx.post(f"{API}/auth/accept-invite", json={"token": token, "name": "Toinen Käsittelijä", "password": pw},
                       timeout=30)
        assert r.status_code == 200, r.text
        made.append(r.json()["user"]["id"])
        return client_for(email, pw)
    made: list[str] = []
    yield make
    for uid in made:
        assert ops().patch(f"/ops/users/{uid}", json={"disabled": True}).status_code == 200


def test_single_user_approves_own_filing_only_with_an_audited_override():
    me = _me(ops())
    others = [u for u in db.users.find({"org_type": "internal", "disabled": {"$ne": True}, "id": {"$ne": me["id"]}})]
    assert not others, "precondition: ops.demo is the only active internal user"
    a = _filing_approval(me)
    assert _listed(a["id"])["four_eyes"] == "single_user"
    r = _decide(ops(), a["id"], note="Checked against TARIC")
    assert r.status_code == 409 and r.json()["code"] == "four_eyes_single_user" and r.json()["override_allowed"] is True
    assert r.json()["detail"].startswith("Four-eyes rule:")
    assert db.approvals.find_one({"id": a["id"]})["status"] == "pending"
    r = _decide(ops(), a["id"], note="short", override=True)
    assert r.status_code == 400 and r.json()["code"] == "four_eyes_override_note"
    r = _decide(ops(), a["id"], note="Only ops user in the company today", override=True)
    assert r.status_code == 200, r.text
    stored = db.approvals.find_one({"id": a["id"]})
    assert stored["status"] == "approved" and stored["four_eyes_override"]["note"] == "Only ops user in the company today"
    assert stored["four_eyes_override"]["by"]["id"] == me["id"]
    log = db.audit_log.find_one({"action": "approval_four_eyes_override", "entity_id": a["ref_id"]})
    assert log and log["actor"]["id"] == me["id"] and log["data"]["approval_id"] == a["id"]


def test_requester_may_reject_own_request():
    a = _filing_approval(_me(ops()))
    r = _decide(ops(), a["id"], decision="rejected", note="Wrong office code")
    assert r.status_code == 200 and r.json()["status"] == "rejected" and not r.json().get("four_eyes_override")


def test_claim_payment_needs_a_colleague_with_claims_approve(colleague):
    fin = colleague("finance")  # claims.approve, but not customs.file
    aid = _claim_approval("approve")
    assert _listed(aid)["four_eyes"] == "colleague"
    r = _decide(ops(), aid, note="Approve the payment")
    assert r.status_code == 409 and r.json()["code"] == "four_eyes_required" and r.json()["override_allowed"] is False
    r = _decide(ops(), aid, note="I want to override this anyway", override=True)
    assert r.status_code == 409 and r.json()["code"] == "four_eyes_required"  # a colleague exists: no override
    r = _decide(fin, aid, note="Toinen henkilö tarkisti")
    assert r.status_code == 200 and r.json()["decided_by"]["id"] != _me(ops())["id"]
    # the finance user does not hold customs.file, so a filing is still a single-user case
    a = _filing_approval(_me(ops()))
    assert _decide(ops(), a["id"], note="x").json()["code"] == "four_eyes_single_user"
    assert _decide(fin, a["id"], note="x").status_code == 403


def test_claim_rejection_proposal_is_not_four_eyes():
    aid = _claim_approval("reject")
    assert _listed(aid)["four_eyes"] is None
    assert _decide(ops(), aid, note="Rejection confirmed").status_code == 200


def test_autopilot_on_keeps_the_single_approver_flow():
    sw = lambda on: ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable"})  # noqa: E731
    aid = _claim_approval("approve")
    assert sw(True).status_code == 200
    try:
        assert _listed(aid)["four_eyes"] is None
        r = _decide(ops(), aid, note="Autopilot on: one approver")
        assert r.status_code == 200, r.text
    finally:
        assert sw(False).status_code == 200
