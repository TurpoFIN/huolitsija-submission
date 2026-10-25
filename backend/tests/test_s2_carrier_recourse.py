"""s2 carrier recourse for paid claims (services/claims_recourse.py, routes/claims.py /recourse): the carrier of the claim's
leg (named leg, else the main leg), the carrier's cap under the leg's regime (CMR 8.33 SDR/kg, Hague-Visby 667 SDR per
package or 2 SDR/kg, Montreal 26 SDR/kg) at the stored SDR rate, amount = min(paid, cap), the fi + en letter, the owner
queue card (kind carrier_recourse) decided through routes.admin.decide, the send rule (carrier e-mail AND
recourse_contract), the explicit ops POST for claims paid earlier (idempotent) and recoveries.

In process against a throw-away MongoDB database (own database, dropped at the end); EMAIL_PROVIDER=log, so no carrier
receives anything. Expected caps are written out by hand (SDR fixture 1.2 EUR), not computed with the code's formula."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_recourse_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"  # never deliver test e-mail, whatever the shell has
os.environ.pop("OWNER_ALERT_EMAIL", None)
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import timedelta  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core import config  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:  # core.config was imported by another test module first: never touch its DB
    pytest.skip("run test_s2_carrier_recourse.py in its own pytest process", allow_module_level=True)

from core.permissions import permissions_for  # noqa: E402

CID = str(uuid.uuid4())
FINANCE = {"id": "fin-1", "email": "talous@huolitsija.example", "name": "Talous", "role": "finance", "org_type": "internal",
           "locale": "fi", "session_version": 0}
OPS = {"id": "ops-1", "email": "ops@huolitsija.example", "name": "Operaattori", "role": "ops_admin", "org_type": "internal",
       "locale": "fi", "session_version": 0}
CUSTOMER = {"id": "cust-1", "email": "asiakas@testi.example", "name": "Asiakas", "role": "customer_admin",
            "org_type": "customer", "company_id": CID, "locale": "fi", "session_version": 0}
CARRIERS = {
    "road": {"id": "car-road", "name": "Tie Kuljetus Oy", "code": "TIEKUL", "contact_email": "booking@tiekuljetus.example",
             "claims_email": "claims@tiekuljetus.example", "recourse_contract": True},
    "sea": {"id": "car-sea", "name": "Meri Linjat Ab", "code": "MERI", "contact_email": "claims@merilinjat.example"},
    "air": {"id": "car-air", "name": "Ilma Cargo", "code": "ILMA"},
}
SDR_EUR = 1.2


def _user(u: dict) -> dict:
    return {**u, "permissions": sorted(permissions_for(u))}


def setup_module(_m):
    async def go():
        from core.db import db, ensure_indexes
        from core.util import now_iso
        import services.claims_recourse  # noqa: F401  (claim.paid and approval.decided handlers)

        await ensure_indexes()
        await db.raw.companies.insert_one({"id": CID, "name": "Takautuma Testi Oy", "business_id": "3990011-1", "country": "FI",
                                           "kyc_status": "approved", "status": "active", "created_at": "2026-01-01T00:00:00Z"})
        for u in (FINANCE, OPS, CUSTOMER):
            await db.raw.users.insert_one(dict(u))
        for c in CARRIERS.values():
            await db.raw.carriers.insert_one({**c, "active": True, "modes": ["road", "sea", "air"]})
        await db.raw.sdr_rates.insert_one({"date": _today().isoformat(), "eur_per_sdr": SDR_EUR, "fetched_at": now_iso(),
                                           "source": "test fixture (IMF not fetched)", "source_url": None})
    run(go())


def teardown_module(_m):
    from core.db import client, db
    run(client.drop_database(db.raw.name))  # the database this module really used
    _LOOP.close()


def _today():
    from services.billing import today_fi
    return today_fi()


def _leg(seq: int, mode: str, carrier: str | None, *, kind: str = "main", doc_no: str | None = None) -> dict:
    c = CARRIERS.get(carrier or "", {})
    return {"seq": seq, "kind": kind, "mode": mode, "carrier_id": c.get("id"), "carrier_name": c.get("name"),
            "transport_doc_no": doc_no, "from": {"name": "Helsinki", "country": "FI"}, "to": {"name": "Hamburg", "country": "DE"}}


async def _paid_claim(tag: str, legs: list[dict], *, mode: str, kg: float, packages: int | None, paid: int, claimed: int,
                      leg_seq: int | None = None, references: dict | None = None, via_mark_paid: bool = True) -> dict:
    """A shipment with legs and an approved claim (decision amount = paid); mark_paid fires claim.paid."""
    from core.db import db
    from core.util import new_id, now_iso
    from services import claims

    sid = new_id()
    await db.raw.shipments.insert_one({"id": sid, "reference": f"HUO-{tag}", "company_id": CID, "mode": mode,
                                       "status": "delivered", "legs": legs, "references": references or {},
                                       "cargo": {"weight_kg": 5000, "packages": 40}, "created_at": "2026-01-02T00:00:00Z"})
    loss = (_today() - timedelta(days=3)).isoformat()
    cl = {"id": new_id(), "number": f"HUO-CLM-{tag}", "shipment_id": sid, "shipment_reference": f"HUO-{tag}",
          "company_id": CID, "company_name": "Takautuma Testi Oy", "type": "damage", "description": "Pallets crushed in transit",
          "amount_claimed_cents": claimed, "currency": "EUR", "gross_kg_affected": kg, "packages_affected": packages,
          "date_of_loss": loss, "delivery_date": loss, "liability_regime": "NSAB", "status": "approved",
          "decision": {"amount_cents": paid, "note": "ok"}, "documents": [{"id": "d1", "filename": f"photos-{tag}.pdf"}],
          "document_ids": ["d1"], "history": [], "created_at": now_iso(), "updated_at": now_iso()}
    if leg_seq is not None:
        cl["leg_seq"] = leg_seq
    if not via_mark_paid:
        cl["status"] = "paid"
        cl["payment"] = {"paid_at": now_iso(), "reference": "earlier"}
    await db.raw.claims.insert_one(dict(cl))
    if via_mark_paid:
        await claims.mark_paid(cl["id"], paid_at=None, reference=f"PAY-{tag}", actor=_user(FINANCE))
    return await db.raw.claims.find_one({"id": cl["id"]}, {"_id": 0})


async def _decide(approval_id: str, decision: str, note: str = "Tarkistettu, lähetetään") -> dict:
    from routes.admin import DecisionIn, decide
    return await decide(approval_id, DecisionIn(decision=decision, note=note), user=_user(FINANCE))


async def _one(coll: str, q: dict) -> dict | None:
    from core.db import db
    return await db.raw[coll].find_one(q, {"_id": 0})


async def _count(coll: str, q: dict) -> int:
    from core.db import db
    return await db.raw[coll].count_documents(q)


def test_road_cmr_recourse_is_queued_then_sent_after_approval_and_recoveries_are_tracked():
    cl = run(_paid_claim("R1", [_leg(1, "road", "road", doc_no="CMR-778812")], mode="road", kg=100, packages=4,
                         paid=200000, claimed=250000))
    r = cl["recourse"]
    # CMR art. 23: 8.33 SDR/kg × 100 kg = 833 SDR; 833 × 1.2 = 999.60 EUR; paid 2 000 EUR -> claim the cap
    assert (r["status"], r["regime"], r["cap_sdr"], r["cap_eur_cents"], r["paid_cents"], r["amount_cents"]) == \
        ("pending_approval", "CMR", 833.0, 99960, 200000, 99960)
    assert (r["carrier_id"], r["carrier_name"], r["transport_doc_no"]) == ("car-road", "Tie Kuljetus Oy", "CMR-778812")
    card = run(_one("approvals", {"id": r["approval_id"]}))
    assert card["kind"] == "carrier_recourse" and card["status"] == "pending" and card["ref_id"] == cl["id"]
    assert card["amount_cents"] == 99960 and card["title_fi"] == "Takautumisvaatimus HUO-CLM-R1: Tie Kuljetus Oy"
    assert "recourse_person" in [x["code"] for x in card["reasons"]]
    fi, en = r["letter_fi"], r["letter_en"]
    assert fi["subject"] == "Takautumisvaatimus: lähetys HUO-R1, CMR CMR-778812 (HUO-CLM-R1)"
    assert en["subject"] == "Recourse claim: shipment HUO-R1, CMR CMR-778812 (HUO-CLM-R1)"
    for needle in ("Vaadittu määrä: 999,60 €", "30 päivän kuluessa", "photos-R1.pdf", "CMR 23 art.: 8,33 SDR × 100 kg",
                   "Vahinkopäivä: "):
        assert needle in fi["body"], needle
    assert "CMR art. 23" not in fi["body"]  # the Finnish letter cites in Finnish
    for needle in ("Amount claimed: EUR 999.60", "within 30 days", "Documents attached:", "photos-R1.pdf", "Date of loss: ",
                   "CMR art. 23: 8.33 SDR × 100 kg gross"):
        assert needle in en["body"], needle
    assert run(_count("email_outbox", {"tag": "claim_recourse"})) == 0  # nothing leaves before a person approves

    run(_decide(r["approval_id"], "approved"))
    got = run(_one("claims", {"id": cl["id"]}))["recourse"]
    assert got["status"] == "sent" and got["sent_to"] == "claims@tiekuljetus.example" and got["awaiting"] is None
    mail = run(_one("email_outbox", {"tag": "claim_recourse", "ref.claim_id": cl["id"]}))
    assert mail["to"] == "claims@tiekuljetus.example" and mail["status"] != "failed"
    assert "Takautumisvaatimus" in mail["subject"] and "Recourse claim" in mail["subject"]
    assert "Vaadittu määrä: 999,60 €" in mail["text"] and "Amount claimed: EUR 999.60" in mail["text"]

    from services import claims_recourse as rc
    with pytest.raises(rc.RecourseError):  # more than was paid to the customer
        run(rc.record_recovery(cl["id"], amount_cents=200001, received_on=None, reference=None, note=None, close=False,
                               actor=_user(FINANCE)))
    run(rc.record_recovery(cl["id"], amount_cents=50000, received_on=None, reference="TK-1", note=None, close=False,
                           actor=_user(FINANCE)))
    got = run(_one("claims", {"id": cl["id"]}))["recourse"]
    assert (got["status"], got["recovered_cents"], len(got["recoveries"])) == ("recovered", 50000, 1)
    run(rc.record_recovery(cl["id"], amount_cents=49960, received_on=_today().isoformat(), reference="TK-2", note="loput",
                           close=True, actor=_user(FINANCE)))
    got = run(_one("claims", {"id": cl["id"]}))["recourse"]
    assert (got["status"], got["recovered_cents"], got["closed_reason"]) == ("closed", 99960, "settled")


def test_sea_hague_visby_paid_below_cap_waits_for_the_contract_flag_then_ops_send():
    cl = run(_paid_claim("S1", [_leg(1, "road", "road", kind="pre"), _leg(2, "sea", "sea")], mode="sea", kg=500,
                         packages=3, paid=150000, claimed=150000, references={"bl_no": "MEDU1234567"}))
    r = cl["recourse"]
    # Merilaki 13:30: max(667 × 3 = 2 001 SDR, 2 × 500 = 1 000 SDR) = 2 001 SDR × 1.2 = 2 401.20 EUR; paid 1 500 EUR
    assert (r["regime"], r["cap_sdr"], r["cap_eur_cents"], r["amount_cents"], r["carrier_id"], r["leg_seq"]) == \
        ("HAGUE_VISBY", 2001.0, 240120, 150000, "car-sea", 2)
    assert r["transport_doc_label"] == "B/L" and "B/L MEDU1234567" in r["letter_en"]["subject"]
    run(_decide(r["approval_id"], "approved"))
    got = run(_one("claims", {"id": cl["id"]}))["recourse"]
    assert (got["status"], got["awaiting"], got["carrier_contract"]) == ("approved", "carrier_contract", False)
    assert run(_count("email_outbox", {"tag": "claim_recourse", "ref.claim_id": cl["id"]})) == 0

    from core.db import db
    from routes import claims as routes
    run(db.raw.carriers.update_one({"id": "car-sea"}, {"$set": {"recourse_contract": True}}))
    out = run(routes.send_recourse(cl["id"], user=_user(FINANCE)))
    assert out["recourse"]["status"] == "sent" and out["recourse"]["sent_to"] == "claims@merilinjat.example"


def test_named_leg_air_montreal_cap_and_no_contact_keeps_it_approved():
    legs = [_leg(1, "road", "road", kind="pre"), _leg(2, "air", "air", doc_no="105-12345675")]
    cl = run(_paid_claim("A1", legs, mode="air", kg=10, packages=1, paid=100000, claimed=120000, leg_seq=2))
    r = cl["recourse"]
    # Montreal art. 22(3) from 28 Dec 2024: 26 SDR/kg × 10 kg = 260 SDR × 1.2 = 312.00 EUR
    assert (r["regime"], r["cap_sdr"], r["cap_eur_cents"], r["amount_cents"], r["carrier_name"]) == \
        ("MONTREAL", 260.0, 31200, 31200, "Ilma Cargo")
    assert r["transport_doc_label"] == "AWB" and r["transport_doc_no"] == "105-12345675"
    run(_decide(r["approval_id"], "approved"))
    got = run(_one("claims", {"id": cl["id"]}))["recourse"]
    assert (got["status"], got["awaiting"]) == ("approved", "carrier_contact")
    assert run(_count("email_outbox", {"tag": "claim_recourse", "ref.claim_id": cl["id"]})) == 0


def test_ops_post_for_an_earlier_paid_claim_is_idempotent_and_a_decline_can_be_redrafted():
    from routes import claims as routes

    cl = run(_paid_claim("P1", [_leg(1, "road", "road")], mode="road", kg=50, packages=None, paid=30000, claimed=30000,
                         via_mark_paid=False))
    assert cl.get("recourse") is None  # paid before this code: no event, nothing drafted
    got = run(routes.get_recourse(cl["id"], user=_user(OPS)))
    assert got["recourse"] is None and got["status"] == "paid"
    first = run(routes.create_recourse(cl["id"], None, user=_user(OPS)))["recourse"]
    # 8.33 × 50 = 416.5 SDR × 1.2 = 499.80 EUR; paid 300 EUR -> claim what was paid
    assert (first["status"], first["cap_eur_cents"], first["amount_cents"]) == ("pending_approval", 49980, 30000)
    again = run(routes.create_recourse(cl["id"], None, user=_user(OPS)))["recourse"]
    assert again["approval_id"] == first["approval_id"]
    assert run(_count("approvals", {"kind": "carrier_recourse", "ref_id": cl["id"]})) == 1
    shown = run(routes.get_recourse(cl["id"], user=_user(OPS)))["recourse"]
    assert shown["approval"]["id"] == first["approval_id"] and shown["approval"]["status"] == "pending"

    run(_decide(first["approval_id"], "rejected", "Ei kannata"))
    got = run(_one("claims", {"id": cl["id"]}))["recourse"]
    assert (got["status"], got["closed_reason"]) == ("closed", "declined")
    redo = run(routes.create_recourse(cl["id"], None, user=_user(OPS)))["recourse"]
    assert redo["status"] == "pending_approval" and redo["approval_id"] != first["approval_id"]
    assert run(_count("approvals", {"kind": "carrier_recourse", "ref_id": cl["id"]})) == 2

    customer_view = run(routes.get_claim(cl["id"], user=_user(CUSTOMER)))
    assert "recourse" not in customer_view  # letters and the carrier claim stay internal


def test_unpaid_claim_has_no_recourse():
    from core.db import db
    from routes import claims as routes

    cl = run(_paid_claim("U1", [_leg(1, "road", "road")], mode="road", kg=10, packages=1, paid=10000, claimed=10000,
                         via_mark_paid=False))
    run(db.raw.claims.update_one({"id": cl["id"]}, {"$set": {"status": "approved"}, "$unset": {"recourse": 1}}))
    with pytest.raises(HTTPException) as e:
        run(routes.create_recourse(cl["id"], None, user=_user(OPS)))
    assert e.value.status_code == 409
