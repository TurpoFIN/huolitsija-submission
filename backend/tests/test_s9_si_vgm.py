"""Shipping instructions, VGM and cut-offs against real counterparts:

- DCSA channel: the DCSA Conformance Gateway (dcsaorg/Conformance-Gateway, run locally) plays the carrier. Our app posts
  eBL 3.0 SI and VGM 1.0 declarations to it, receives its notifications on our callback, serves its VGM pull, and the
  gateway's own conformance checker must end each scenario CONFORMANT.
- E-mail channel: the SI/VGM go to the carrier desk's sandbox mailbox (GreenMail); the test reads them over IMAP, checks
  the attachments against the official DCSA schema, and answers as the desk.

Env (scripts in /home/user/work): CARRIER_MAERSK_* (DCSA sandbox), DCSA_EBL_SANDBOX, DCSA_VGM_SANDBOX, CARRIER_HAPAG_BOOKING_DESK_EMAIL.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from test_s9_support import cleanup_test_shipments, create_test_shipment, db, ops, set_autopilot, work

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import dcsa_sandbox  # noqa: E402

SPEC = ROOT / "backend" / "data" / "dcsa"
EBL_SB, VGM_SB = os.environ.get("DCSA_EBL_SANDBOX"), os.environ.get("DCSA_VGM_SANDBOX")
needs_gateway = pytest.mark.skipif(not (EBL_SB and VGM_SB), reason="DCSA gateway sandboxes not configured")


def official(spec_file: str, component: str, payload: dict) -> list[str]:
    doc = yaml.safe_load((SPEC / spec_file).read_text())
    v = Draft202012Validator({"$ref": f"#/components/schemas/{component}", "components": doc["components"]})
    return [e.message for e in v.iter_errors(payload)]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


PARTIES = {"shipper": {"name": "TEST S9 Lahden Kaluste Oy", "contact_name": "Liisa Laine", "email": "liisa@kaluste.sandbox.test",
                       "address": {"street": "Askonkatu 9", "postal_code": "15100", "city": "Lahti", "country": "FI"}},
           "consignee": {"name": "TEST S9 Shanghai Home Co", "address": {"street": "88 Century Ave", "postal_code": "200120",
                                                                          "city": "Shanghai", "country": "CN"}}}


def sea_shipment(carrier: str, containers: int = 1) -> dict:
    return create_test_shipment(mode="sea", origin="FIKTK", destination="CNSHA", carrier=carrier, service="FCL", incoterm="FOB",
                                etd=iso(datetime.now(timezone.utc) + timedelta(days=5)), parties=PARTIES,
                                cargo={"description": "TEST S9 flat-packed furniture", "packages": 300, "package_type": "cartons",
                                       "weight_kg": 9000 * containers, "volume_m3": 60 * containers, "hs_code": "940360",
                                       "containers": [{"type": "40HC", "count": containers}]},
                                references={"customer_ref": "TEST-S9"})


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    set_autopilot(False)
    yield
    set_autopilot(False)
    ids = [s["id"] for s in db.shipments.find({"test": True}, {"id": 1})]
    for coll in ("execution_records", "shipping_instructions", "vgm_declarations"):
        db[coll].delete_many({"shipment_id": {"$in": ids}})
    cleanup_test_shipments()


def start(sid: str, ref: str, carrier_code: str, cutoffs=None) -> dict:
    r = ops().post(f"/execution/shipments/{sid}/start", json={"carrier_booking_reference": ref, "carrier_code": carrier_code,
                                                              "vessel": "TEST VESSEL", "voyage": "642E", "cutoffs": cutoffs,
                                                              "vgm_signatory": "Liisa Laine"})
    assert r.status_code == 200, r.text
    return r.json()


def fill_container(sid: str, number: str, seal: str, tare: float, cargo: float | None = 9000) -> dict:
    body = {"equipment_reference": number, "seal_number": seal, "tare_kg": tare}
    if cargo:
        body["cargo_gross_kg"] = cargo
    r = ops().post(f"/execution/shipments/{sid}/containers/0", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def due_now(kind: str, sid: str):
    db.jobs.update_many({"kind": kind, "payload.shipment_id": sid}, {"$set": {"next_run_at": "2000-01-01T00:00:00Z", "status": "queued"}})


def imap_fetch(address: str, subject_part: str, tries: int = 20):
    for _ in range(tries):
        box = imaplib.IMAP4("127.0.0.1", 3143)
        box.login(address, "sandbox")
        box.select("INBOX")
        _, data = box.search(None, "ALL")
        found = None
        for num in reversed(data[0].split()):
            _, msg = box.fetch(num, "(RFC822)")
            m = email.message_from_bytes(msg[0][1], policy=email.policy.default)
            if subject_part in m["Subject"]:
                found = m
                break
        box.logout()
        if found:
            return found
        time.sleep(0.5)
    raise AssertionError(f"no mail with {subject_part} for {address}")


def reply_as_desk(original, desk: str, text: str):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = desk, "toteutus@huolitsija.test", "Re: " + original["Subject"]
    m["In-Reply-To"] = original["Message-ID"]
    m["References"] = original["Message-ID"]
    m["Message-ID"] = email.utils.make_msgid(domain="hapag-lloyd.sandbox.test")
    m.set_content(text + "\n\n> " + original.get_body(("plain",)).get_content()[:200])
    with smtplib.SMTP("127.0.0.1", 3025) as s:
        s.send_message(m)


# ------------------------------------------------------------------------------------------------- schema + ISO 6346

def test_container_number_check_digit_and_cutoff_derivation():
    from services.execution import cutoffs, dcsa

    assert dcsa.iso6346_valid("CSQU3054383") and not dcsa.iso6346_valid("CSQU3054384") and not dcsa.iso6346_valid("CSQ3054383")
    built = cutoffs.build([{"code": "DCO", "at": "2026-11-01T10:00:00Z"}], "2026-11-03T12:00:00Z")
    by = {c["code"]: c for c in built}
    assert by["DCO"]["source"] == "carrier" and by["DCO"]["at"].startswith("2026-11-01T10:00")
    assert by["VCO"]["source"] == "derived" and by["VCO"]["at"].startswith("2026-11-02T12:00")


def test_invalid_container_number_is_refused():
    s = sea_shipment("Hapag-Lloyd")
    start(s["id"], "TESTHL0000001", "HAPAG")
    r = ops().post(f"/execution/shipments/{s['id']}/containers/0", json={"equipment_reference": "CSQU3054384"})
    assert r.status_code == 400 and "check digit" in r.text


# ------------------------------------------------------------------------------------------------- DCSA channel

@needs_gateway
def test_si_dcsa_sandbox_end_to_end_conformant():
    gw = dcsa_sandbox.Gateway()
    dcsa_sandbox.start_scenario(gw, EBL_SB, "UC1[Sea Waybill] - GET SI")
    s = sea_shipment("Maersk")
    v = start(s["id"], f"TESTMSK{int(time.time()) % 10**7}", "MAERSK")
    assert v["record"]["channel"] == "dcsa" and len(v["record"]["containers"]) == 1
    # Autopilot on, data missing: the job waits (deferral) and names what is missing
    set_autopilot(True)
    due_now("execution.si_submit", s["id"])
    work(["execution.si_submit"])
    rec = db.execution_records.find_one({"shipment_id": s["id"]})
    assert {m["code"] for m in rec["si_missing"]} >= {"container_number", "seal"}
    deferred = db.jobs.find_one({"kind": "execution.si_submit", "payload.shipment_id": s["id"]})
    assert deferred["status"] == "queued" and deferred["defer_count"] >= 1
    assert deferred["next_run_at"] > iso(datetime.now(timezone.utc))
    fill_container(s["id"], "CSQU3054383", "ML123456", 3900)
    due_now("execution.si_submit", s["id"])
    work(["execution.si_submit"])
    si = db.shipping_instructions.find_one({"shipment_id": s["id"]}, sort=[("version", -1)])
    assert si["status"] in ("submitted", "received"), si.get("http")  # RECEIVED may already be in
    assert si["http"]["status"] == 202 and si["carrier_reference"]
    assert not official("EBL_v3.0.0.yaml", "CreateShippingInstructions", si["payload"])
    dec = db.agent_decisions.find_one({"subject_id": s["id"], "action": "si_submit"}, sort=[("at", -1)])
    assert dec["outcome"] == "auto"
    # the gateway's carrier notifies RECEIVED on our callback
    for _ in range(40):
        si = db.shipping_instructions.find_one({"id": si["id"]})
        if si.get("carrier_status") == "RECEIVED":
            break
        time.sleep(0.5)
    assert si.get("carrier_status") == "RECEIVED" and si["status"] == "received"
    gw.complete_current_action(EBL_SB)
    r = ops().post(f"/execution/si/{si['id']}/refresh")
    assert r.status_code == 200 and db.shipping_instructions.find_one({"id": si["id"]})["last_get"]["status"] == 200
    st = None
    for _ in range(20):
        st = dcsa_sandbox.current_status(gw, EBL_SB, "UC1[Sea Waybill] - GET SI")
        if st.get("conformanceStatus") == "CONFORMANT" and not st.get("isRunning"):
            break
        if st.get("isRunning") and not st.get("nextActions"):
            gw.complete_current_action(EBL_SB)
        time.sleep(0.5)
    assert st["conformanceStatus"] == "CONFORMANT", st.get("errors")
    # cut-off DCO now done
    v = ops().get(f"/execution/shipments/{s['id']}").json()
    assert next(c for c in v["cutoffs"] if c["code"] == "DCO")["state"] == "done"


@needs_gateway
def test_vgm_dcsa_push_and_pull_conformant():
    gw = dcsa_sandbox.Gateway()
    dcsa_sandbox.start_scenario(gw, VGM_SB, "POST VGM Declaration")
    s = sea_shipment("Maersk")
    ref = f"TESTMSKV{int(time.time()) % 10**6}"
    start(s["id"], ref, "MAERSK")
    fill_container(s["id"], "CSQU3054383", "ML654321", 3900, cargo=8950)
    r = ops().post(f"/execution/shipments/{s['id']}/vgm")
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["status"] == "accepted" and res["http"]["status"] == 200, res
    decl = db.vgm_declarations.find_one({"shipment_id": s["id"]})
    assert decl["method"] == "SM2" and decl["weight_kg"] == 12850.0
    assert not official("VGM_v1.0.0.yaml", "VGMDeclaration", decl["declaration"])
    st = dcsa_sandbox.current_status(gw, VGM_SB, "POST VGM Declaration")
    if st.get("isRunning"):
        gw.complete_current_action(VGM_SB)
        st = dcsa_sandbox.current_status(gw, VGM_SB, "POST VGM Declaration")
    assert st["conformanceStatus"] == "CONFORMANT", st.get("errors")
    # pull: the carrier asks our endpoint for declarations of this booking
    name = "Supply parameters (carrierBookingReference) - GET VGM Declaration"
    dcsa_sandbox.start_scenario(gw, VGM_SB, name)
    st = dcsa_sandbox.current_status(gw, VGM_SB, name)
    if st.get("inputRequired"):
        gw.handle_action_input(VGM_SB, st["scenario_id"], st["promptActionId"], {"carrierBookingReference": ref})
    for _ in range(30):
        st = dcsa_sandbox.current_status(gw, VGM_SB, name)
        if st.get("conformanceStatus") == "CONFORMANT" and not st.get("isRunning"):
            break
        if st.get("isRunning") and not st.get("inputRequired"):
            try:
                gw.complete_current_action(VGM_SB)
            except dcsa_sandbox.GatewayError:
                pass
        time.sleep(0.5)
    assert st["conformanceStatus"] == "CONFORMANT", st.get("errors")
    v = ops().get(f"/execution/shipments/{s['id']}").json()
    assert next(c for c in v["cutoffs"] if c["code"] == "VCO")["state"] == "done"


# ------------------------------------------------------------------------------------------------- e-mail channel

def test_si_by_email_to_booking_desk_and_desk_reply():
    s = sea_shipment("Hapag-Lloyd")
    ref = f"TESTHL{int(time.time()) % 10**7}"
    v = start(s["id"], ref, "HAPAG")
    assert v["record"]["channel"] == "email"
    fill_container(s["id"], "HLXU6173427", "HL778899", 3880)
    set_autopilot(False)
    # Autopilot off: the agent may not send; an owner-queue card appears instead
    due_now("execution.si_submit", s["id"])
    work(["execution.si_submit"])
    card = db.approvals.find_one({"kind": "execution_review", "ref_id": s["id"], "status": "pending"})
    assert card and card["data"]["action"] == "si_submit"
    assert db.shipping_instructions.find_one({"shipment_id": s["id"]})["status"] == "awaiting_approval"
    r = ops().post(f"/approvals/{card['id']}/decide", json={"decision": "approved", "note": "TEST S9"})
    assert r.status_code == 200, r.text
    si = None
    for _ in range(20):
        si = db.shipping_instructions.find_one({"shipment_id": s["id"], "status": "submitted"})
        if si:
            break
        time.sleep(0.3)
    assert si and si["channel"] == "email" and si["email_status"] == "sent"
    desk = "si-desk.hapag-lloyd-sandbox-test@sandbox.test"
    m = imap_fetch(desk, ref)
    atts = {p.get_filename(): p.get_payload(decode=True) for p in m.iter_attachments()}
    js = next(v for k, v in atts.items() if k.endswith(".json"))
    payload = json.loads(js)
    assert not official("EBL_v3.0.0.yaml", "CreateShippingInstructions", payload)
    assert payload["consignmentItems"][0]["carrierBookingReference"] == ref
    assert payload["utilizedTransportEquipments"][0]["equipmentReference"] == "HLXU6173427"
    assert payload["freightPaymentTermCode"] == "COL"  # FOB: buyer pays the sea freight
    reply_as_desk(m, "si-desk@hapag-lloyd.sandbox.test", "Well noted, SI received. Draft B/L follows.")
    work(["execution.mail_poll"], fire=True)
    assert db.shipping_instructions.find_one({"id": si["id"]})["status"] == "received"


def test_vgm_by_email_with_vermas_and_rejection_reply():
    s = sea_shipment("Hapag-Lloyd")
    ref = f"TESTHLV{int(time.time()) % 10**6}"
    start(s["id"], ref, "HAPAG")
    fill_container(s["id"], "HLXU1849740", "HL112233", 3880)
    r = ops().post(f"/execution/shipments/{s['id']}/containers/0", json={"weighbridge_gross_kg": 12990, "certificate": "VAAKA-77"})
    assert r.status_code == 200
    r = ops().post(f"/execution/shipments/{s['id']}/vgm")
    assert r.json()["result"]["status"] == "submitted", r.text
    m = imap_fetch("si-desk.hapag-lloyd-sandbox-test@sandbox.test", f"VGM {ref}")
    atts = {p.get_filename(): p.get_payload(decode=True).decode() for p in m.iter_attachments()}
    edi = next(v for k, v in atts.items() if k.endswith(".edi"))
    assert "UNH+1+VERMAS:D:16A:UN:SMDG10'" in edi and "EQD+CN+HLXU1849740" in edi and "MEA+AAE+VGM+KGM:12990'" in edi
    assert "DOC+SM1:VGM:306'" in edi and f"RFF+BN:{ref}'" in edi
    vj = json.loads(next(v for k, v in atts.items() if k.endswith(".json")))
    assert not official("VGM_v1.0.0.yaml", "PostVGMDeclarationsRequest", vj)
    reply_as_desk(m, "si-desk@hapag-lloyd.sandbox.test", "VGM rejected: weight missing for container, please correct.")
    work(["execution.mail_poll"], fire=True)
    rec = db.execution_records.find_one({"shipment_id": s["id"]})
    assert rec["containers"][0]["vgm_status"] == "rejected" and not rec.get("vgm_submitted_at")
    assert db.execution_alerts.find_one({"code": "vgm_rejected", "subject_id": s["id"]})


# ------------------------------------------------------------------------------------------------- cut-offs + kill switch

def test_cutoff_watch_alerts_once_per_threshold_and_on_miss():
    now = datetime.now(timezone.utc)
    s = sea_shipment("Hapag-Lloyd")
    start(s["id"], f"TESTHLC{int(time.time()) % 10**6}", "HAPAG",
          cutoffs=[{"code": "DCO", "at": iso(now + timedelta(hours=5))}, {"code": "VCO", "at": iso(now + timedelta(hours=30))},
                   {"code": "FCO", "at": iso(now - timedelta(hours=1))}])
    work(["execution.cutoff_watch"], fire=True)
    alerts = list(db.execution_alerts.find({"subject_id": s["id"]}))
    keys = {a["key"].split(":", 2)[-1] for a in alerts}
    assert keys == {"DCO:6", "VCO:48", "FCO:missed"}, keys  # 5 h left: one 6 h alert, not 6+24+48
    rec = db.execution_records.find_one({"shipment_id": s["id"]})
    assert {c["code"]: c["state"] for c in rec["cutoffs"]} == {"DCO": "due_soon", "VCO": "due_soon", "FCO": "missed"}
    n = len(alerts)
    work(["execution.cutoff_watch"], fire=True)
    assert db.execution_alerts.count_documents({"subject_id": s["id"]}) == n  # no duplicates


def test_kill_switch_holds_agent_jobs():
    s = sea_shipment("Hapag-Lloyd")
    start(s["id"], f"TESTHLK{int(time.time()) % 10**6}", "HAPAG")
    fill_container(s["id"], "CSQU3054383", "HL445566", 3880)
    set_autopilot(True, halted=True)
    try:
        due_now("execution.si_submit", s["id"])
        work(["execution.si_submit"])
        assert db.jobs.find_one({"kind": "execution.si_submit", "payload.shipment_id": s["id"]})["status"] == "queued"
        assert not db.shipping_instructions.find_one({"shipment_id": s["id"]})
    finally:
        set_autopilot(False, halted=False)


def test_vgm_does_not_treat_quote_weight_as_measured():
    s = sea_shipment('Hapag-Lloyd')
    start(s['id'], 'TEST-UNMEASURED-' + s['id'][:8], 'HAPAG')
    fill_container(s['id'], 'CSQU3054383', 'TEST-SEAL', 3880, cargo=None)
    result = ops().post(f"/execution/shipments/{s['id']}/vgm").json()['result']
    assert result['status'] == 'waiting'
    assert 'cargo_weight' in {m['code'] for m in result['missing']}
    assert not db.vgm_declarations.find_one({'shipment_id': s['id']})


def test_unchanged_si_is_not_sent_twice():
    s = sea_shipment('Hapag-Lloyd')
    start(s['id'], 'TEST-IDEMPOTENT-' + s['id'][:8], 'HAPAG')
    fill_container(s['id'], 'CSQU3054383', 'TEST-SEAL', 3880)
    first = ops().post(f"/execution/shipments/{s['id']}/si").json()['result']
    second = ops().post(f"/execution/shipments/{s['id']}/si").json()['result']
    assert first['status'] == 'submitted' and first['id'] == second['id']
    assert db.execution_messages.count_documents({'ref_id': first['id'], 'direction': 'out'}) == 1
