"""Domain repair regressions against MongoDB, actual services and published payment examples.

Model outputs are controlled only at core.llm._call_openai. No claim of real-model quality.
Run with the CI backend; uses the same DB_NAME and V4_API environment as the other suites.
"""
import copy
import hashlib
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
import pytest_asyncio
import asyncio

@pytest_asyncio.fixture(autouse=True, scope="module", loop_scope="module")
async def motor_loop():
    from core.db import client
    client._io_loop = asyncio.get_running_loop()
    yield

from fastapi import HTTPException

from core import config, llm
from core.db import db
from core.refs import fi_reference, rf_reference
from core.settings import DEFAULTS, get_settings, update_settings
from core.tenant_visibility import hidden_test_companies
from core.util import NO_ID, new_id, now_iso
from services import billing, customs, messaging, partner_network
from services.customs_decisions import decision_values, record_decision
from services.invoice_pdf import virtual_barcode

BOT = {"id": "test-domain-operator", "name": "Domain test operator", "org_type": "internal"}


def test_published_finnish_reference_example():
    # Finanssiala, Kotimaisen viitenumeron rakenne, p.2 (2009-11-01).
    assert fi_reference("123456") == "12 34561"
    assert rf_reference("86851 62596 19897") == "RF09868516259619897"
    assert rf_reference("55958 22432 94671") == "RF06559582243294671"


@pytest.mark.parametrize("iban,amount,ref,due,expected", [
    # Finanssiala Pankkiviivakoodi-opas 5.3 pp.7–8: independent published test invoices.
    ("FI79 4405 2020 0360 82", 488315, "86851 62596 19897", "2010-06-12",
     "479440520200360820048831500000000868516259619897100612"),
    ("FI58 1017 1000 0001 22", 48299, "55958 22432 94671", "2012-01-31",
     "458101710000001220004829900000000559582243294671120131"),
    ("FI79 4405 2020 0360 82", 488315, "RF09 8685 1625 9619 897", "2010-06-12",
     "579440520200360820048831509000000868516259619897100612"),
    ("FI58 1017 1000 0001 22", 48299, "RF06 5595 8224 3294 671", "2010-01-31",
     "558101710000001220004829906000000559582243294671100131"),
])
def test_published_barcode_examples(iban, amount, ref, due, expected):
    assert virtual_barcode(iban, amount, ref, due) == expected
    assert len(expected) == 54


@pytest.mark.parametrize("changes", [
    {"iban": None}, {"iban": "FI7944052020036083"}, {"amount": -1}, {"amount": 100000000},
    {"reference": "12345"}, {"reference": "RF09559582243294671"}, {"due": "bad-date"}, {"due": "2100-01-01"},
])
def test_invalid_payment_input_is_missing_not_zero(changes):
    args = {"iban": "FI7944052020036082", "amount": 488315, "reference": "868516259619897", "due": "2010-06-12"}
    args.update(changes)
    assert virtual_barcode(args["iban"], args["amount"], args["reference"], args["due"]) is None


def test_decision_requires_all_explicit_authority_values():
    d = {"mrn": "26FI123456789012R4", "items": [{"seq": 1}, {"seq": 2}]}
    body = {"mrn": d["mrn"], "decision_date": date.today().isoformat(), "paid_by": "forwarder",
            "items": [{"seq": 1, "duty_cents": 0, "vat_cents": 0}, {"seq": 2, "duty_cents": 101, "vat_cents": 202}]}
    assert decision_values(d, body)["duty_cents"] == 101
    for change in ({"items": body["items"][:1]}, {"mrn": "26FI999999999999R9"}, {"paid_by": None},
                   {"decision_date": ""}, {"items": [body["items"][0], body["items"][0]]},
                   {"items": [body["items"][0], {"seq": 2, "duty_cents": None, "vat_cents": 202}]}):
        with pytest.raises(HTTPException):
            decision_values(d, {**body, **change})


@pytest.mark.parametrize("lane,sell,expected", [("road:FI>SE", 100000, 80000), ("road:FI>SE", 200000, 90000),
                                              ("road:FI>SE", 1, 0)])
def test_partner_margin_and_maximum_boundaries(lane, sell, expected):
    settings = {**DEFAULTS, "partner_approved_lanes": ["road:FI>SE"], "partner_target_margin_pct": 20,
                "partner_max_price_cents": 90000}
    assert partner_network.cost_limit(settings, lane, sell) == expected
    with pytest.raises(HTTPException):
        partner_network.cost_limit(settings, "road:SE>FI", sell)


@pytest.mark.asyncio(loop_scope="module")
async def test_estimate_is_not_a_disbursement_and_decided_values_own_invoice():
    cid, sid, did = new_id(), new_id(), new_id()
    company = {"id": cid, "name": "Domain billing test", "test_tenant": True}
    shipment = {"id": sid, "company_id": cid, "status": "in_transit", "reference": "DOMAIN-TEST"}
    decl = {"id": did, "company_id": cid, "shipment_id": sid, "mrn": "26FI123456789012R4", "type": "import",
            "status": "released", "vat_route": "paid_to_customs", "items": [{"seq": 1}],
            "totals": {"estimate": True, "duty_cents": 999999, "vat_cents": 888888}}
    await db.companies.insert_one(company)
    await db.shipments.insert_one(shipment)
    await db.customs_declarations.insert_one(copy.deepcopy(decl))
    warnings, info = [], []
    assert await billing._customs_lines(shipment, "fi", warnings, info) == []
    assert warnings and info[0]["code"] == "CUSTOMS_DECISION_PENDING"
    forged = {"id": new_id(), "company_id": cid, "shipment_id": sid, "lines": [{"code": "DUTY", "source_ref": did,
               "qty": 1, "amount_cents": 999999}]}
    with pytest.raises(billing.BillingError):
        await billing.validate_customs_invoice(forged)
    updated = await record_decision(decl, {"mrn": decl["mrn"], "decision_date": date.today().isoformat(), "paid_by": "forwarder",
          "items": [{"seq": 1, "duty_cents": 1234, "vat_cents": 5678}]}, BOT)
    lines = await billing._customs_lines(shipment, "fi", [], [])
    assert [(x["code"], x["amount_cents"]) for x in lines] == [("DUTY", 1234), ("IMPORT_VAT", 5678)]
    assert updated["totals"]["estimate"] is True  # original calculation remains honestly estimated
    inv = {"id": new_id(), "company_id": cid, "shipment_id": sid, "kind": "invoice", "status": "open", "lines": lines}
    await billing.validate_customs_invoice(inv)
    changed = copy.deepcopy(inv); changed["lines"][0]["amount_cents"] += 1
    with pytest.raises(billing.BillingError):
        await billing.validate_customs_invoice(changed)
    await db.invoices.insert_one(copy.deepcopy(inv))
    with pytest.raises(billing.BillingError):
        await billing.validate_customs_invoice({**inv, "id": new_id()})
    with pytest.raises(HTTPException):
        await record_decision(updated, {}, BOT)
    assert await db.audit_log.find_one({"entity_id": did, "action": "customs_decision_recorded"})
    for collection, query in [("invoices", {"id": inv["id"]}), ("customs_declarations", {"id": did}),
                              ("shipments", {"id": sid}), ("companies", {"id": cid})]:
        await db[collection].delete_many(query)


def valid_declaration():
    cid, sid, did = new_id(), new_id(), new_id()
    company = {"id": cid, "name": "Domain customs fixture", "business_id": "3990001-4", "eori": "FI3990001-4",
               "kyc_status": "approved", "sanctions_status": "clear", "vat_registered": True, "test_tenant": True,
               "poa": {"type": "direct", "scope": ["import"], "valid_from": "2020-01-01",
                       "valid_to": "2099-01-01", "signed_at": now_iso()}}
    shipment = {"id": sid, "company_id": cid, "status": "in_transit", "reference": "DOMAIN-TEST",
                "origin": {"country": "CN"}, "destination": {"country": "FI"}}
    importer = {"name": company["name"], "eori": company["eori"], "vat_id": "FI39900014"}
    decl = {"id": did, "company_id": cid, "shipment_id": sid, "lrn": "LRN-DOMAIN-TEST", "type": "import",
            "status": "ready_for_review", "declaration_date": "2026-10-24", "created_at": now_iso(),
            "submitter": {"name": "Domain test broker", "email": "broker@domain-test.example"}, "transaction_nature": "11",
            "representative": {"name": "Domain fixture forwarder", "eori": "FI3990002-2"}, "representative_status_code": "2",
            "representation_type": "direct", "importer": importer, "declarant": dict(importer),
            "exporter": {"name": "Domain fixture exporter", "address": {"street": "Test Street 1", "city": "Shanghai",
                         "postal_code": "200000", "country": "CN"}},
            "delivery_terms": {"incoterm": "FOB", "place": "Shanghai"},
            "documents": [{"type": "commercial_invoice", "code": "N380", "number": "DOMAIN-INVOICE"}],
            "invoice": {"document_id": new_id(), "confirmed": True, "currency": "EUR", "total": 1000},
            "office": {"code": "FI002000"}, "location_of_goods": "Local test terminal",
            "goods_location": {"type": "D", "address": {"street": "Testikatu 1", "city": "Helsinki",
                               "postal_code": "00100", "country": "FI"}},
            "transport": {"transport_document": {"type": "bill_of_lading", "number": "DOMAIN-BL"},
                          "mode_at_border": "1", "inland_mode": "1", "nationality": "FI",
                          "dispatch_country": "CN", "destination_country": "FI"},
            "valuation": {"invoice_currency": "EUR", "invoice_total": 1000, "freight_known": True,
                          "freight_to_border_cents": 10000, "insurance_cents": 0, "post_border_cents": 0,
                          "exchange_rate": {"rate": 1}},
            "items": [{"seq": 1, "description": "Non-motorised aluminium bicycles", "hs_code": "871200",
                       "cn_code": "87120030", "taric_code": "8712003010", "cn_confirmed": True, "origin_country": "CN",
                       "invoice_amount": 1000, "gross_kg": 100, "net_kg": 90, "packages": 2, "package_type": "CT",
                       "marks": "DOMAIN TEST 1-2", "quantity": 2, "unit": "p/st", "preference": "100",
                       "documents": [{"code": "Y152", "reference": "Local test importer origin declaration", "source": "declared"}]}]}
    return company, shipment, decl


@pytest.mark.asyncio(loop_scope="module")
async def test_anti_dumping_blocks_approval_and_clearance_is_audited(monkeypatch):
    from services import taric, screening
    from services.customs_screening import screen_parties
    from core.sandbox import sandbox_eori
    from core.agent_runtime import agent_bot
    # Replay unchanged official TARIC pages; only external sanctions responses are controlled.
    fix = Path(__file__).parent / "fixtures/taric"
    pages = {}
    for url, meta in json.loads((fix / "manifest.json").read_text())["pages"].items():
        data = (fix / meta["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == meta["sha256"]
        pages[url] = data.decode()
    monkeypatch.setattr(taric, "_REPLAY", pages)
    # The captured pages answer for their capture date only; assess and file the declaration on that date, as the
    # real code assesses on the filing day (UCC art. 85). Without this the replay misses from the next day on.
    captured = {c["date"] for c in json.loads((fix / "manifest.json").read_text())["cases"] if c["code"] == "8712003010"}
    assert len(captured) == 1
    on = captured.pop()
    monkeypatch.setattr(customs, "now_iso", lambda: on + now_iso()[10:])
    async def lists(**kw):
        return [{"list": key, "version": "domain-test-v1", "loaded": True, "status": "ok",
                 "fetched_at": now_iso()} for key in ("eu", "un")]
    async def screen(name, **kw):
        assert name
        return {"status": "clear", "complete": True, "hits": []}
    monkeypatch.setattr(screening, "refresh_lists", lists)
    monkeypatch.setattr(screening, "lists_status", lists)
    monkeypatch.setattr(screening, "screen_name", screen)
    company, shipment, d = valid_declaration()
    actor = {**BOT, "id": new_id(), "role": "ops_admin"}
    original = await get_settings()
    await db.users.insert_one(dict(actor))
    await db.companies.insert_one(company)
    await db.shipments.insert_one(shipment)
    rep = sandbox_eori("3990002-2")
    await db.raw.aeo_eori_checks.update_one({"eori": rep["eori"]}, {"$set": rep}, upsert=True)
    d["screening"] = await screen_parties(d, kind="import")
    await db.customs_declarations.insert_one(copy.deepcopy(d))
    try:
        await update_settings({"autopilot_enabled": False})
        with pytest.raises(HTTPException) as exc:
            await customs.request_filing(d, actor)
        assert "Anti-dumping" in exc.value.detail
        assert {v["rule"] for v in customs.blocking(d)} == {"TD-1"}, customs.blocking(d)
        with pytest.raises(HTTPException):
            await customs.apply_patch(d, {"items": [{"seq": 1, "trade_defence_clearance_reason": "bad"}]}, actor)
        # A generic clearance note cannot remove an actual official measure. Confirm the published residual code.
        cleared = await customs.apply_patch(d, {"items": [{"seq": 1, "taric_additional_codes": ["B999"],
            "trade_defence_clearance_reason": "Captured TARIC R2146/25: explicit residual B999 at 48.50 percent"}]}, actor)
        assert not customs.blocking(cleared), customs.blocking(cleared)
        assert cleared["items"][0]["trade_defence_cleared_by"]["id"] == actor["id"]
        assert cleared["items"][0]["duty_cents"] == 68750  # 1100 EUR * (14 + 48.5) percent
        pending = await customs.request_filing(cleared, actor)
        approval = await db.approvals.find_one({"id": pending["approval_id"]})
        assert approval["status"] == "pending"
        assert await db.audit_log.find_one({"entity_id": d["id"], "action": "customs_edited"})
        # Standing delegation does not bypass AEO review of this first, trade-defence filing.
        await update_settings({"autopilot_enabled": True, "customs_filing_delegated": True})
        auto = await customs.request_filing(pending, agent_bot("customs_agent"))
        assert auto["status"] == "ready_for_review" and not auto.get("submission")
        assert (await db.approvals.find_one({"id": auto["approval_id"]}))["status"] == "pending"
        assert not await db.autonomy_actions.find_one({"entity_id": d["id"], "kind": "customs_approved"})
    finally:
        await update_settings(original)
        await db.customs_declarations.delete_one({"id": d["id"]})
        await db.companies.delete_one({"id": company["id"]})
        await db.shipments.delete_one({"id": shipment["id"]})
        await db.users.delete_one({"id": actor["id"]})


@pytest.mark.asyncio(loop_scope="module")
async def test_tenant_filter_covers_records_and_aggregate_counts():
    cid, sid, iid = new_id(), new_id(), new_id()
    await db.companies.insert_one({"id": cid, "name": "Domain hidden tenant", "test_tenant": True})
    await db.shipments.insert_one({"id": sid, "company_id": cid, "status": "booked"})
    await db.invoices.insert_one({"id": iid, "company_id": cid, "status": "open"})
    token = hidden_test_companies.set([cid])
    try:
        assert await db.shipments.find_one({"id": sid}) is None
        assert await db.invoices.count_documents({"company_id": cid}) == 0
        assert await db.companies.find({"id": cid}, NO_ID).to_list(None) == []
        assert await db.shipments.aggregate([{"$match": {"company_id": cid}}, {"$count": "n"}]).to_list(None) == []
    finally:
        hidden_test_companies.reset(token)
    assert await db.invoices.count_documents({"company_id": cid}) == 1
    await db.shipments.delete_one({"id": sid});await db.invoices.delete_one({"id": iid});await db.companies.delete_one({"id": cid})


@pytest.mark.asyncio(loop_scope="module")
async def test_autonomy_confirmation_messages_partner_limits_and_switch_off(monkeypatch):
    monkeypatch.setenv("PARTNER_OUTREACH_LIVE", "false")
    # One partner address per run: the per-partner daily send cap (partner_send_counters, Helsinki day) is real
    # state that earlier runs on the same day use up, so each run uses its own address.
    contact = f"rates-{new_id()[:8]}@example.invalid"
    monkeypatch.setenv("PARTNER_OUTREACH_ALLOWLIST", contact)
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "log")
    from core.autonomy import switch, quote_within_policy, validate_settings
    original = await get_settings()
    await update_settings({"autopilot_enabled": False, "partner_network_enabled": False})
    with pytest.raises(HTTPException):
        await switch("autopilot_enabled", True, "yes", BOT)
    with pytest.raises(HTTPException):
        validate_settings({"autopilot_enabled": True})
    assert (await partner_network.run_campaign())["status"] == "off"  # sourcing switch off: nothing runs
    thread = await messaging.get_or_create_thread(company_id="domain-agent-test", shipment_id=None, subject="Policy test", created_by=BOT)
    msg = await messaging.add_message(thread, body="Human approval required", author={"type": "ai", "name": "Test AI"}, needs_approval=True, notify=False)
    assert msg["status"] == "pending_approval"
    bypass = await messaging.add_message(thread, body="Caller cannot bypass", author={"type": "ai", "name": "Test AI"}, needs_approval=False, notify=False)
    assert bypass["status"] == "pending_approval"
    await switch("autopilot_enabled", True, "enable", BOT)
    msg2 = await messaging.add_message(thread, body="Autonomous reply", author={"type": "ai", "name": "Test AI"}, needs_approval=True, notify=False)
    assert msg2["status"] == "sent" and await db.autonomy_actions.find_one({"entity_id": msg2["id"]})
    settings = await get_settings()
    quote = {"request": {"cargo": {}}, "options": [{"total_cents": settings["autopilot_quote_max_cents"], "cost_cents": 1}]}
    assert quote_within_policy(quote, settings)
    quote["options"][0]["total_cents"] += 1
    assert not quote_within_policy(quote, settings)
    await switch("partner_network_enabled", True, "enable", BOT)
    await update_settings({"partner_approved_lanes": ["road:FI>SE"], "partner_target_margin_pct": 20, "partner_max_price_cents": 90000})
    row = {"id": new_id(), "carrier_id": new_id(), "carrier_name": "Controlled provider fixture", "lane": "road:FI>SE",
           "contact_email": contact, "basis": "PER_SHIPMENT", "service": "ROAD_FTL", "sell_cents": 100000, "rounds": 0, "log": []}
    await db.partner_outreach.insert_one(copy.deepcopy(row))
    # The real email adapter is log mode locally; no external message is sent.
    assert config.EMAIL_PROVIDER == "log"
    counter = await partner_network.process_offer(row, {"cost_cents": 80001}, BOT)
    # first counteroffer starts partner_counter_anchor_pct (5 %) below the 800,00 ceiling
    assert counter["status"] == "counteroffered" and counter["limit_cents"] == 80000 and counter["counter_cents"] == 76000
    assert await db.email_outbox.find_one({"ref.id": row["id"], "status": "logged"})
    # The Sourcing switch, not Autopilot, decides: inside the limits the rate is accepted with Autopilot off too.
    await switch("autopilot_enabled", False, "", BOT)
    rate = await partner_network.process_offer(row, {"cost_cents": 80000, "transit_days": 3, "valid_to": (date.today()+timedelta(days=10)).isoformat()}, BOT)
    assert rate["status"] == "accepted" and rate["unit_cost_cents"] == 80000
    row2 = {**row, "id": new_id()};await db.partner_outreach.insert_one(copy.deepcopy(row2))
    await switch("autopilot_enabled", True, "enable", BOT)
    accepted = await partner_network.process_offer(row2, {"cost_cents": 80000, "transit_days": 3, "valid_to": (date.today()+timedelta(days=10)).isoformat()}, BOT)
    assert accepted["status"] == "accepted"
    # Container rates must satisfy the existing price-card contract, not only be stored.
    row3 = {**row, "id": new_id(), "lane": "sea:FI>SE", "service": "FCL", "basis": "PER_CONTAINER", "equipment_type": "40HC"}
    await update_settings({"partner_approved_lanes": ["road:FI>SE", "sea:FI>SE"]})
    await db.partner_outreach.insert_one(copy.deepcopy(row3))
    container = await partner_network.process_offer(row3, {"cost_cents": 70000, "transit_days": 3, "valid_to": (date.today()+timedelta(days=10)).isoformat()}, BOT)
    from services.pricing import price_card
    lines = price_card(container, {"containers": [{"type": "40HC", "count": 1}], "weight_kg": 1000, "volume_m3": 20,
                       "is_fcl": True}, km=100, kind="main", leg_seq=1, freight_code="OCEAN_FREIGHT")
    assert lines[0]["unit_cost_cents"] == 70000

    await switch("partner_network_enabled", False, "", BOT)
    with pytest.raises(HTTPException):  # switch off: no automatic processing
        await partner_network.process_offer(row, {"cost_cents": 1})
    row4 = {**row, "id": new_id()};await db.partner_outreach.insert_one(copy.deepcopy(row4))
    pending = await partner_network.process_offer(row4, {"cost_cents": 70000, "transit_days": 3, "valid_to": (date.today()+timedelta(days=10)).isoformat()}, BOT)
    assert pending["status"] == "pending"  # a person may record an offer while off; it never prices quotes
    await switch("autopilot_enabled", False, "", BOT)
    assert await db.audit_log.find_one({"action": "autonomy_switch_changed", "data.after": False, "actor.id": BOT["id"]})
    await update_settings(original)
