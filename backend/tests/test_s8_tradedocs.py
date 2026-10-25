"""Trade documents (s8): every document generated from real shipments through the real API, the structured exports
validated against the official public schemas (DCSA eBL 3.0.3, UN/CEFACT eCMR D23B, IATA ONE Record 3.3 ontology, the
Cargo-XML 3.00 house waybill sample), numbering, versions, amendments and their audit trail, access scoping, signed
links and the milestone agent with the Autopilot gate. No mocks: the API, MongoDB, GridFS and the PDFs are real.

Run against a local API (default http://localhost:8103, V3_API / V3_DB / V3_DEMO_PASSWORD as in test_v3_support):
    ~/work/venv/bin/python -m pytest -q backend/tests/test_s8_tradedocs.py
"""
import base64
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest
from lxml import etree
from pypdf import PdfReader

import test_v3_support as S

SPECS = os.path.join(S.BACKEND, "data", "specs")
KINDS = ["booking_confirmation", "commercial_invoice", "packing_list", "house_bl", "hawb", "cmr", "certificate_of_origin",
         "eur1_application", "dg_declaration", "arrival_notice", "delivery_order"]


def bearer(email):
    c = S.client()
    r = c.post("/auth/login", json={"email": email, "password": S.PASSWORD})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = "Bearer " + r.json()["token"]
    return c


@pytest.fixture(scope="module")
def ops():
    return bearer(S.OPS)


@pytest.fixture(scope="module")
def cust():
    return bearer(S.CUSTOMER)


def pdf_text(blob: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(blob)).pages)


def ship_id(ref: str) -> str:
    s = S.mongo().shipments.find_one({"reference": ref}, {"id": 1})
    assert s, f"demo shipment {ref} missing (SEED_DEMO must be on)"
    return s["id"]


def test_seq_numbers_and_idempotent_issue(ops):
    sid = ship_id("HUO-2026-000005")
    a = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "booking_confirmation"})
    assert a.status_code == 200, a.text
    b = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "booking_confirmation"})
    assert b.json()["id"] == a.json()["id"], "issuing twice must return the current version, not a new number"
    assert re.fullmatch(r"BC-\d{4}-\d{6}", a.json()["doc_no"])


# One real demo shipment per transport document; the PDF must carry the number and the parties in both languages.
CASES = [
    ("HUO-2026-000001", "house_bl", r"HUOB\d{8}", ["Ningbo Beilun Home Furnishing", "Pohjolan Kaluste Oy", "HLXU"],
     ("KONOSSEMENTTI", "BILL OF LADING")),
    ("HUO-2026-000008", "hawb", r"HUOA\d{8}", ["Taichung Brake Systems", "157-", "TPE"], ("LENTORAHTIKIRJA", "HOUSE AIR WAYBILL")),
    ("HUO-2026-000003", "cmr", r"HUOC\d{8}", ["Wielkopolska Meble", "Poznań", "Lahti"], ("KANSAINVÄLINEN RAHTIKIRJA", "INTERNATIONAL CONSIGNMENT NOTE")),
    ("HUO-2026-000009", "dg_declaration", r"DG-\d{4}-\d{6}", ["UN3480", "Lithium ion batteries", "Franken eDrive"],
     ("VAARALLISTEN AINEIDEN", "MULTIMODAL DANGEROUS GOODS FORM")),
    ("HUO-2026-000001", "arrival_notice", r"AN-\d{4}-\d{6}", ["Pohjolan Kaluste Oy", "Kotka"], ("SAAPUMISILMOITUS", "ARRIVAL NOTICE")),
    ("HUO-2026-000005", "delivery_order", r"DO-\d{4}-\d{6}", [], ("LUOVUTUSMÄÄRÄYS", "DELIVERY ORDER")),
    ("HUO-2026-000005", "commercial_invoice", r"CI-\d{4}-\d{6}", ["NORA"], ("KAUPPALASKU", "COMMERCIAL INVOICE")),
    ("HUO-2026-000001", "packing_list", r"PL-\d{4}-\d{6}", ["412"], ("PAKKAUSLUETTELO", "PACKING LIST")),
    ("HUO-2026-000005", "certificate_of_origin", r"CO-\d{4}-\d{6}", ["VN"], ("ALKUPERÄTODISTUS", "CERTIFICATE OF ORIGIN")),
]


@pytest.mark.parametrize("ref,kind,pattern,needles,titles", CASES)
def test_document_pdf_in_finnish_and_english(ops, ref, kind, pattern, needles, titles):
    sid = ship_id(ref)
    # The seeded shipment has no carrier/terminal release authority. Its DO is a draft.
    # The release contract suite separately proves final issuance against labelled source evidence.
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": kind, "final": kind != "delivery_order"})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert re.fullmatch(pattern, doc["doc_no"]), doc["doc_no"]
    assert doc["status"] == ("draft" if kind == "delivery_order" else "issued") and doc["current"] and doc["version"] >= 1
    for lang, title in zip(("fi", "en"), titles):
        p = ops.get(f"/trade-documents/{doc['id']}/pdf?lang={lang}")
        assert p.status_code == 200 and p.content[:4] == b"%PDF"
        text = pdf_text(p.content)
        assert doc["doc_no"] in text and title in text, (lang, text[:400])
        for n in needles:
            assert n in text, (n, lang)


def test_documents_that_do_not_apply_are_refused_with_the_reason(ops):
    sid = ship_id("HUO-2026-000001")  # sea import from China
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "cmr"})
    assert r.status_code == 409 and "road" in r.json()["detail"]
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "dg_declaration"})
    assert r.status_code == 409 and "dangerous" in r.json()["detail"].lower()
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "eur1_application"})
    assert r.status_code == 409


# ------------------------------------------------------------------------------------------------ official schemas
def _export(ops, ref, kind, fmt):
    sid = ship_id(ref)
    doc = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": kind}).json()
    r = ops.get(f"/trade-documents/{doc['id']}/data?format={fmt}")
    assert r.status_code == 200, r.text
    return doc, r


def test_house_bl_is_a_valid_dcsa_ebl_3_transport_document(ops):
    from openapi_schema_validator import OAS30Validator
    doc, r = _export(ops, "HUO-2026-000001", "house_bl", "dcsa_ebl_3")
    td = r.json()
    schema = json.load(open(os.path.join(SPECS, "dcsa_ebl_3.0.3_TransportDocument.json")))
    errors = list(OAS30Validator(schema).iter_errors(td))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]
    assert td["transportDocumentReference"] == doc["doc_no"]
    assert td["transportDocumentTypeCode"] == "SWB" and td["isToOrder"] is False
    assert all(e["equipment"]["equipmentReference"].startswith("HLXU") for e in td["utilizedTransportEquipments"])
    assert td["documentParties"]["shipper"]["partyName"].startswith("Ningbo Beilun")


def test_cmr_is_a_valid_uncefact_ecmr(ops):
    doc, r = _export(ops, "HUO-2026-000003", "cmr", "ecmr")
    schema = json.load(open(os.path.join(SPECS, "uncefact_eCMR_D23B.json")))
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(r.json()))
    assert not errors, [e.message[:200] for e in errors[:3]]
    c = r.json()["specifiedConsignment"][0]
    assert r.json()["exchangedDocument"]["typeCode"]["content"] == "730"
    assert c["consignor"]["name"][0]["content"].startswith("Wielkopolska")
    assert c["carrierAcceptanceLogisticsLocation"]["id"][0]["content"] == "PLPOZ"


def test_hawb_one_record_uses_only_iata_ontology_terms(ops):
    import rdflib
    from rdflib.namespace import RDF
    doc, r = _export(ops, "HUO-2026-000008", "hawb", "one_record")
    onto = rdflib.Graph().parse(os.path.join(SPECS, "iata_onerecord_cargo_3.3.ttl"))
    cargo = "https://onerecord.iata.org/ns/cargo#"
    terms = {str(s) for s in onto.subjects() if str(s).startswith(cargo)}
    g = rdflib.Graph().parse(data=json.dumps(r.json()), format="json-ld")
    unknown = {str(x) for s, p, o in g for x in (p, o) if str(x).startswith(cargo) and str(x) not in terms}
    assert not unknown, unknown
    wb = next(s for s in g.subjects(RDF.type, rdflib.URIRef(cargo + "Waybill"))
              if (s, rdflib.URIRef(cargo + "waybillNumber"), rdflib.Literal(doc["doc_no"])) in g)
    assert (wb, rdflib.URIRef(cargo + "waybillType"), rdflib.URIRef(cargo + "HOUSE")) in g


def test_hawb_xfzb_matches_cargo_xml_house_waybill_structure(ops):
    doc, r = _export(ops, "HUO-2026-000008", "hawb", "xfzb")
    root = etree.fromstring(r.content)
    assert root.tag == "{iata:housewaybill:1}HouseWaybill"
    sample = etree.parse(os.path.join(SPECS, "xfzb_sample_riege.xml")).getroot()
    known = {etree.QName(e).localname for e in sample.iter()} | {"DeclaredValueForCustomsAmount", "GrossVolumeMeasure"}
    used = {etree.QName(e).localname for e in root.iter()}
    assert used <= known, used - known
    ns = {"d": "iata:datamodel:3", "h": "iata:housewaybill:1"}
    assert root.findtext("h:BusinessHeaderDocument/d:ID", namespaces=ns) == doc["doc_no"]
    assert root.findtext("h:MasterConsignment/d:TransportContractDocument/d:ID", namespaces=ns).startswith("157-")
    assert root.findtext("h:MasterConsignment/d:IncludedHouseConsignment/d:OriginLocation/d:ID", namespaces=ns) == "TPE"


# ------------------------------------------------------------------------------------------------ amendments
def test_amendment_new_version_old_superseded_and_audited(ops, cust):
    sid = ship_id("HUO-2026-000001")
    doc = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "arrival_notice"}).json()
    new_contact = f"PK-REF-{uuid.uuid4().hex[:6].upper()}"
    r = ops.post(f"/trade-documents/{doc['doc_no']}/amend",
                 json={"changes": {"refs.customer": new_contact}, "reason": "Consignee contact corrected"})
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["doc_no"] == doc["doc_no"] and v2["version"] == doc["version"] + 1 and v2["current"]
    assert v2["amendment"]["changes"][0]["new"] == new_contact and v2["amendment"]["reason"] == "Consignee contact corrected"
    hist = ops.get(f"/trade-documents/{doc['doc_no']}/versions").json()
    by_v = {v["version"]: v for v in hist["versions"]}
    assert by_v[doc["version"]]["status"] == "superseded" and not by_v[doc["version"]]["current"]
    assert any(a["action"] == "tradedoc.amended" and a["data"]["reason"] == "Consignee contact corrected" for a in hist["audit"])
    text = pdf_text(ops.get(f"/trade-documents/{v2['id']}/pdf?lang=fi").content)
    assert new_contact in text and f"Korjaus {v2['amendment']['number']}" in text
    ver = ops.get(f"/trade-documents/verify/{doc['doc_no']}?v={doc['version']}").json()
    assert ver["genuine"] and not ver["is_current"] and ver["current_version"] == v2["version"]
    # guards
    assert ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={"changes": {"issuer.name": "X"}, "reason": "try"}).status_code == 400
    assert ops.post(f"/trade-documents/{doc['doc_no']}/amend",
                    json={"changes": {"refs.customer": new_contact}, "reason": "again"}).status_code == 409
    assert ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={"changes": {"consignee.name": "Y"}, "reason": ""}).status_code == 400
    for changes in ({"shipment": {"id": "other"}}, {"shipment.company_id": "other"}, {"dates.issue": "1990-01-01"}, {"made_up": "value"}):
        assert ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={"changes": changes, "reason": "forbidden identity"}).status_code == 400
    assert cust.post(f"/trade-documents/{doc['doc_no']}/amend",
                     json={"changes": {"consignee.name": "Y"}, "reason": "customer"}).status_code == 403


def test_customer_sees_own_documents_only(ops, cust):
    me = cust.get("/auth/me").json()["user"]
    own = S.mongo().shipments.find_one({"company_id": me["company_id"], "demo": True}, {"id": 1})
    ops.post(f"/shipments/{own['id']}/trade-documents", json={"kind": "booking_confirmation"})
    lst = cust.get(f"/shipments/{own['id']}/trade-documents").json()
    assert lst["documents"] and "available" not in lst
    assert all("to" not in d and "email_id" not in d for doc in lst["documents"] for d in doc.get("dispatches", []))
    d = lst["documents"][0]
    assert cust.get(f"/trade-documents/{d['id']}/pdf").status_code == 200
    other = S.mongo().trade_documents.find_one({"company_id": {"$ne": me["company_id"]}}, {"id": 1})
    assert cust.get(f"/trade-documents/{other['id']}").status_code == 404
    assert cust.get(f"/trade-documents/{other['id']}/pdf").status_code == 404


def test_signed_links_work_without_sign_in_and_reject_tampering(ops):
    import sys
    sys.path.insert(0, S.BACKEND)
    issued = ops.post(f"/shipments/{ship_id('HUO-2026-000001')}/trade-documents", json={"kind": "house_bl"})
    assert issued.status_code == 200, issued.text
    doc = issued.json()
    anon = S.client()
    from services.tradedocs import dispatch
    # Use the same environment as the local API; inventing a test-only secret
    # makes otherwise valid links fail against the server's configured signer.
    exp = int(time.time()) + 600
    good = f"/trade-documents/{doc['id']}/download/pdf_en?exp={exp}&sig={dispatch.sign(doc['id'], 'pdf_en', exp)}"
    r = anon.get(good)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert anon.get(good.replace("pdf_en", "pdf_fi", 1)).status_code == 403
    old = int(time.time()) - 5
    assert anon.get(f"/trade-documents/{doc['id']}/download/pdf_en?exp={old}&sig={dispatch.sign(doc['id'], 'pdf_en', old)}").status_code == 403


# ------------------------------------------------------------------------------------------------ the milestone agent
def _fresh_shipment(ops, ref_src="HUO-2026-000003") -> str:
    """A real, non-demo copy of a demo road import, so the agent (which leaves demo shipments to people) acts on it."""
    db = S.mongo()
    src = db.shipments.find_one({"reference": ref_src}, {"_id": 0})
    sid = str(uuid.uuid4())
    src.update(id=sid, reference=f"HUO-TEST-{sid[:6].upper()}", demo=False, test=True, status="booked",
               created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    for lg in src["legs"]:
        lg.update(atd=None, ata=None, status="planned")
    src["milestones"] = [m for m in src["milestones"] if m["code"] == "booked"]
    src["parties"]["shipper"]["email"] = f"shipper.{sid[:6]}@example.pl"
    db.shipments.insert_one(dict(src))
    return sid


def _autopilot(ops, on: bool):
    r = ops.post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable" if on else ""})
    assert r.status_code == 200, r.text


def _run_milestones(ops, sid):
    # A status event may already be processing this shipment in the real worker.
    # Only the documented lock-conflict response is retryable.
    deadline = time.monotonic() + 20
    while True:
        response = ops.post(f"/shipments/{sid}/trade-documents/run")
        if response.status_code != 409 or response.json().get("detail") != "Trade documents are being processed. Retry shortly.":
            break
        assert time.monotonic() < deadline, response.text
        time.sleep(0.1)
    assert response.status_code == 200, response.text
    result = response.json()
    assert "milestones" in result, result
    return result


def test_agent_issues_per_milestone_and_gates_outside_parties(ops):
    db = S.mongo()
    sid = _fresh_shipment(ops)
    _autopilot(ops, False)
    try:
        out = _run_milestones(ops, sid)
        assert out["milestones"] == ["booking_confirmed"]
        kinds = {d["kind"]: d for d in db.trade_documents.find({"shipment_id": sid, "current": True}, {"_id": 0})}
        assert kinds["booking_confirmation"]["status"] == "issued"
        assert kinds["booking_confirmation"]["issued_by"]["type"] == "agent"
        assert kinds["booking_confirmation"]["issued_by"]["id"] == "trade_documents"
        assert kinds["cmr"]["status"] == "draft"  # drafted at booking for the parties to check
        assert any(d["role"] == "customer" for d in kinds["booking_confirmation"]["dispatches"])
        assert "house_bl" not in kinds  # a road shipment's ro-ro ferry leg stays under the CMR
        # Autopilot off: the CMR draft goes to the customer's own users only; the shipper (outside party) waits for the owner
        assert not any(d["role"] == "shipper" for d in kinds["cmr"]["dispatches"])
        assert db.approvals.find_one({"kind": "trade_document", "ref_id": kinds["cmr"]["id"], "status": "pending"})
        # pickup -> the CMR is due as a final document; Autopilot off -> owner decides, the draft stays
        assert ops.post(f"/shipments/{sid}/status", json={"status": "picked_up"}).status_code == 200
        out = _run_milestones(ops, sid)
        assert "picked_up" in out["milestones"]
        assert any(w["kind"] == "cmr" for w in out["waiting"])
        assert db.trade_documents.find_one({"shipment_id": sid, "kind": "cmr", "current": True})["status"] == "draft"
        # Autopilot on: the agent issues the final CMR (version 2, same number) and sends it to the shipper too
        _autopilot(ops, True)
        out = _run_milestones(ops, sid)
        cmr = db.trade_documents.find_one({"shipment_id": sid, "kind": "cmr", "current": True}, {"_id": 0})
        assert cmr["status"] == "issued" and cmr["version"] == 2 and cmr["source"] == "autopilot", out
        roles = {d["role"] for d in cmr["dispatches"]}
        assert "shipper" in roles and roles & {"customer", "consignee"}, cmr["dispatches"]  # the customer is the consignee
        sent = db.email_outbox.find_one({"ref.id": cmr["id"], "to": f"shipper.{sid[:6]}@example.pl"})
        assert sent and sent["subject"].startswith("CMR")
        assert db.audit_log.find_one({"entity": "trade_document", "entity_id": cmr["id"], "action": "tradedoc.dispatched"})
        # idempotent: a second run sends nothing new
        again = _run_milestones(ops, sid)
        assert not again["sent"] and not again["issued"]
    finally:
        _autopilot(ops, False)


# ------------------------------------------------------------------------------------------------ eBL issuance (DCSA)
def test_ebl_issuance_request_matches_dcsa_issuance_api(ops):
    import sys
    sys.path.insert(0, S.BACKEND)
    import jwt
    import yaml
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from openapi_schema_validator import OAS30Validator
    from services.tradedocs import ebl
    # Other suites can leave incomplete B/Ls in the shared CI database. Issue
    # this test's document from the named, complete demo shipment through the API.
    r = ops.post(f"/shipments/{ship_id('HUO-2026-000001')}/trade-documents", json={"kind": "house_bl"})
    assert r.status_code == 200, r.text
    # The public response omits structured payloads; the signer consumes the
    # stored document, selected by the exact ID returned above.
    doc = S.mongo().trade_documents.find_one({"id": r.json()["id"]}, {"_id": 0})
    pdf = ops.get(f"/trade-documents/{doc['id']}/pdf?lang=en")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    to = {"partyName": doc["data"]["shipper"]["name"][:70], "sendToPlatform": "WAVE",
          "identifyingCodes": [{"codeListProvider": "WAVE", "partyCode": "shipper-account-1"}]}
    req = ebl.build_issuance_request(doc, pdf.content, pem=pem, kid="huolitsija-ebl-1", to=to)
    spec = yaml.safe_load(open("/home/user/work/specs/dcsa/DCSA_EBL_ISS_3.0.3_resolved.yaml")) \
        if os.path.exists("/home/user/work/specs/dcsa/DCSA_EBL_ISS_3.0.3_resolved.yaml") else None
    if spec:
        schema = {"components": spec["components"], "$ref": "#/components/schemas/IssuanceRequest"}
        errors = list(OAS30Validator(schema).iter_errors(req))
        assert not errors, [f"{list(e.path)}: {e.message}"[:200] for e in errors[:5]]
    manifest = jwt.decode(req["issuanceManifestSignedContent"], key.public_key(), algorithms=["ES256"])
    assert manifest["documentChecksum"] == ebl.checksum(req["document"])
    assert manifest["issueToChecksum"] == ebl.checksum(to)
    assert manifest["eBLVisualisationByCarrierChecksum"] == ebl.checksum(req["eBLVisualisationByCarrier"]["content"])


def test_jcs_rfc8785_vectors():
    import sys
    sys.path.insert(0, S.BACKEND)
    from services.tradedocs.ebl import canonical
    # RFC 8785 section 3.2.2 / 3.2.3 examples
    assert canonical({"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
                      "string": "\u20ac$\u000f\u000aA'\u0042\u0022\u005c\\\"/", "literals": [None, True, False]}) == \
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],' \
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    assert canonical({"\u20ac": 1, "\r": 2, "1": 3, "\U0001F600": 4, "\u0080": 5, "\u00f6": 6}) == \
        '{"\\r":2,"1":3,"\u0080":5,"ö":6,"€":1,"😀":4}'


def test_master_bl_draft_never_impersonates_carrier_issue(ops):
    sid = ship_id("HUO-2026-000001")
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "master_bl", "final": True})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["status"] == "draft" and doc["draft_only"]
    en = pdf_text(ops.get(f"/trade-documents/{doc['id']}/pdf?lang=en").content)
    assert "MASTER BILL OF LADING" in en and "not a carrier-issued" in en
    amended = ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={
        "changes": {"refs.customer": "MBL-REVIEW-" + uuid.uuid4().hex[:6]}, "reason": "Correct carrier review instructions"})
    assert amended.status_code == 200, amended.text
    assert amended.json()["status"] == "draft" and amended.json()["version"] == doc["version"] + 1


def test_logged_mail_is_not_reported_as_sent_and_can_retry(ops):
    status = ops.get("/integrations/status")
    assert status.status_code == 200, status.text
    if status.json()["email"]["provider"] != "log":
        pytest.skip("log-provider behavior applies only to log environment")
    # Historical provider receipts do not select the running provider. Use a new
    # shipment so a previous real delivery cannot trigger successful-send dedup.
    sid = _fresh_shipment(ops, "HUO-2026-000005")
    doc = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "booking_confirmation"}).json()
    for _ in range(2):
        r = ops.post(f"/trade-documents/{doc['id']}/send")
        assert r.status_code == 200, r.text
        assert not r.json()["sent"] and r.json()["logged"]


def test_send_waits_for_atomic_document_lease_before_any_email(ops):
    db = S.mongo()
    sid = _fresh_shipment(ops, "HUO-2026-000005")
    doc = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "booking_confirmation"}).json()
    key = f"{sid}:booking_confirmation"
    db.trade_document_locks.insert_one({"_id": key, "owner": "test-concurrent-worker",
                                       "until": datetime.now(timezone.utc) + timedelta(minutes=2)})
    before = db.email_outbox.count_documents({"ref.id": doc["id"]})
    try:
        r = ops.post(f"/trade-documents/{doc['id']}/send")
        assert r.status_code == 409, r.text
        assert db.email_outbox.count_documents({"ref.id": doc["id"]}) == before
    finally:
        db.trade_document_locks.delete_one({"_id": key, "owner": "test-concurrent-worker"})
    r = ops.post(f"/trade-documents/{doc['id']}/send")
    assert r.status_code == 200, r.text
    assert db.email_outbox.count_documents({"ref.id": doc["id"]}) > before


def test_description_amendment_and_source_refresh_keep_consignment_totals(ops):
    sid = ship_id("HUO-2026-000005")
    doc = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "packing_list"}).json()
    totals = doc["data"]["totals"]
    assert totals["packages"] == 120
    description = doc["data"]["goods"][0]["description"] + " (packing description checked)"
    r = ops.post(f"/trade-documents/{doc['doc_no']}/amend", json={
        "changes": {"goods.0.description": description}, "reason": "Clarify packing description"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["totals"] == totals
    refreshed = ops.post(f"/trade-documents/{doc['doc_no']}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["data"]["totals"]["packages"] == 120


def test_incomplete_invoice_is_draft_only_and_complete_source_has_actual_total(ops):
    # Pick a fresh shipment without invoice/declaration source rows.
    sid = _fresh_shipment(ops, "HUO-2026-000001")
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "commercial_invoice"})
    assert r.status_code == 409 and "amount" in r.text
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "commercial_invoice", "final": False})
    assert r.status_code == 200 and r.json()["status"] == "draft"
    good = ops.post(f"/shipments/{ship_id('HUO-2026-000005')}/trade-documents", json={"kind": "commercial_invoice"}).json()
    assert good["data"]["totals"]["amount"] == 8810.0
    assert good["data"]["totals"]["currency"] == "USD"


def test_invoice_seller_and_lines_share_source_document(ops):
    sid = ship_id("HUO-2026-000005")
    r = ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "commercial_invoice"})
    assert r.status_code == 200, r.text
    doc = r.json()
    source = S.mongo().documents.find_one({"id": doc["data"]["source_document_id"]})
    assert source and doc["data"]["shipper"]["name"] == source["extraction"]["fields"]["seller_name"]["value"]
    assert source["extraction"]["fields"]["seller_name"]["value"] in pdf_text(ops.get(f"/trade-documents/{doc['id']}/pdf?lang=en").content)


def test_concurrent_issue_keeps_one_current_document(ops):
    from concurrent.futures import ThreadPoolExecutor
    sid = _fresh_shipment(ops)
    def issue_one(_):
        return ops.post(f"/shipments/{sid}/trade-documents", json={"kind": "booking_confirmation"})
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(issue_one, range(6)))
    assert all(r.status_code in (200, 409) for r in responses)
    ok = [r.json() for r in responses if r.status_code == 200]
    assert ok and len({d["id"] for d in ok}) == 1
    assert S.mongo().trade_documents.count_documents({"shipment_id": sid, "kind": "booking_confirmation", "current": True}) == 1
