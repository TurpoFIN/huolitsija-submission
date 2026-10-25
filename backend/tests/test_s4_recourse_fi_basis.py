"""Carrier recourse letter in Finnish uses a Finnish cap formula ("667 SDR × 1 kolli"), with FI/EN plurals.
Live HUO-CLM-2026-000004 (TEST) printed the English "667 SDR × 1 packages" inside the Finnish letter.
Pure checks on compute_cap, plus the letter refresh for a draft saved before this change, against a throw-away MongoDB
database (MONGO_URL; skipped when none is reachable). EMAIL_PROVIDER=log: nothing is sent."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s4_recourse_fi_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"
os.environ.pop("OWNER_ALERT_EMAIL", None)
import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import date  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

import pytest  # noqa: E402

from core import config  # noqa: E402
from services import claims  # noqa: E402

ON = date(2026, 10, 24)


def run(coro):
    return _LOOP.run_until_complete(coro)


def _hv(packages, kg):
    return claims.compute_cap(regime_key="HAGUE_VISBY", claim_type="damage", gross_kg=kg, packages=packages,
                              forwarder_role="principal", freight_cents=None, eur_per_sdr=1.2, on=ON)


def test_hague_visby_basis_has_finnish_formula_and_plurals():
    one = _hv(1, 14.1)
    assert one["basis"] == "Merilaki 674/1994 13:30: max(667 SDR × 1 package, 2 SDR × 14.1 kg)"
    assert one["basis_fi"] == "Merilaki 674/1994 13:30: suurempi arvoista 667 SDR × 1 kolli ja 2 SDR × 14,1 kg"
    three = _hv(3, 500)
    assert "667 SDR × 3 packages" in three["basis"] and "667 SDR × 3 kollia" in three["basis_fi"]


def test_cmr_and_intermediary_basis_in_finnish():
    cmr = claims.compute_cap(regime_key="CMR", claim_type="damage", gross_kg=100, packages=4, forwarder_role="principal",
                             freight_cents=None, eur_per_sdr=1.2, on=ON)
    assert cmr["basis_fi"] == "CMR 23 art.: 8,33 SDR × 100 kg bruttopainoa"
    mid = claims.compute_cap(regime_key="NSAB", claim_type="damage", gross_kg=100, packages=4, forwarder_role="intermediary",
                             freight_cents=None, eur_per_sdr=1.2, on=ON)
    assert mid["basis_fi"] == "PSYM 2015 § 23 (välitysliike): 50 000 SDR toimeksiantoa kohden"


def test_old_draft_letters_are_refreshed_text_only():
    if config.DB_NAME != os.environ["DB_NAME"]:
        pytest.skip("run in its own pytest process")
    from core.db import client, db
    try:
        run(client.admin.command("ping"))
    except Exception:  # noqa: BLE001
        pytest.skip("no local MongoDB")
    from services import claims_recourse as recourse
    try:
        run(db.raw.sdr_rates.insert_one({"date": ON.isoformat(), "eur_per_sdr": 1.21091, "fetched_at": "2026-10-24T12:00:00Z",
                                          "source": "test fixture", "source_url": None}))
        run(db.raw.carriers.insert_one({"id": "car-sea", "name": "TEST Meri Linjat Ab", "code": "TMERI", "active": True}))
        sid, cid = str(uuid.uuid4()), str(uuid.uuid4())
        run(db.raw.shipments.insert_one({"id": sid, "reference": "HUO-TEST-42", "company_id": "c1", "mode": "sea",
                                         "status": "delivered", "references": {},
                                         "legs": [{"seq": 1, "kind": "main", "mode": "sea", "carrier_id": "car-sea",
                                                   "carrier_name": "TEST Meri Linjat Ab"}]}))
        claim = {"id": cid, "number": "HUO-CLM-TEST-4", "shipment_id": sid, "shipment_reference": "HUO-TEST-42",
                 "company_id": "c1", "company_name": "TEST Oy", "type": "damage", "amount_claimed_cents": 25000,
                 "gross_kg_affected": 14.1, "packages_affected": 1, "date_of_loss": ON.isoformat(),
                 "delivery_date": ON.isoformat(), "status": "paid", "decision": {"amount_cents": 25000},
                 "payment": {"paid_at": "2026-10-25T01:58:06Z"}, "documents": [], "history": []}
        run(db.raw.claims.insert_one(dict(claim)))
        draft = run(recourse.build(claim))
        assert "667 SDR × 1 kolli ja 2 SDR × 14,1 kg" in draft["letter_fi"]["body"]
        assert "packages" not in draft["letter_fi"]["body"] and "1 package," in draft["letter_en"]["body"]
        # A draft saved before the Finnish basis existed: English formula in the Finnish letter, card pending.
        old = {**draft, "status": "pending_approval", "approval_id": "card-1", "cap_basis_fi": None,
               "cap_basis": "Merilaki 674/1994 13:30: max(667 SDR × 1 packages, 2 SDR × 14.1 kg)",
               "letter_fi": {"subject": "s", "body": "Vastuunrajoitus: 667 SDR × 1 packages"}}
        old.pop("cap_basis_fi")
        run(db.raw.claims.update_one({"id": cid}, {"$set": {"recourse": old}}))
        cl = run(db.raw.claims.find_one({"id": cid}, {"_id": 0}))
        out = run(recourse.refresh_letters(cl))["recourse"]
        stored = run(db.raw.claims.find_one({"id": cid}, {"_id": 0}))["recourse"]
        for r in (out, stored):
            assert "667 SDR × 1 kolli" in r["letter_fi"]["body"] and "packages" not in r["letter_fi"]["body"]
            assert (r["status"], r["approval_id"], r["cap_sdr"], r["amount_cents"]) == ("pending_approval", "card-1", 667.0, 25000)
    finally:
        run(client.drop_database(db.raw.name))
