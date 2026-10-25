"""Supplier payments against the real local API (s13): payables from approved carrier invoices, payment runs with
ISO 20022 pain.001 validated against the official XSD, Autopilot approval and its stop reasons, pain.002 status
reports and camt.054 debit notifications with realised FX checked against the ECB rate stored in fx_rates.

Carrier invoices are written straight to MongoDB in the contract shape (test=True), as agent A builds that module.
Bank files (pain.002, camt.054) are written here from the ISO 20022 message definitions, independently of the code
under test, and validated against the official XSDs before they are posted.
"""
from __future__ import annotations

import time
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from lxml import etree

from test_s13_support import db, iso, now, ops
from test_v4_support import fi_check_digit, rf_valid

XSD_DIR = Path(__file__).resolve().parent.parent / "data" / "sepa"
DEBTOR_IBAN, DEBTOR_BIC = "FI2112345600000785", "NDEAFIHH"   # valid mod-97 example IBAN, Nordea Finland BIC


def xsd(name: str) -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(XSD_DIR / f"{name}.xsd")))


def assert_valid(xml: bytes, name: str):
    schema = xsd(name)
    doc = etree.fromstring(xml)
    ok = schema.validate(doc)
    assert ok, [str(e) for e in schema.error_log][:5]


def iban_ok(iban: str) -> bool:  # independent mod-97 check (ISO 13616)
    s = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


def fi_ref(base: str) -> str:
    return base + str(fi_check_digit(base))


def rf_ref(fi: str) -> str:
    num = "".join(str(int(c, 36)) for c in fi + "RF00")
    return f"RF{98 - int(num) % 97:02d}{fi}"


def next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


EXEC = next_weekday(date.today() + timedelta(days=3))


@pytest.fixture(scope="module", autouse=True)
def company_bank_account():
    before = ops().get("/settings").json()
    r = ops().patch("/settings", json={"seller_iban": DEBTOR_IBAN, "seller_bic": DEBTOR_BIC})
    assert r.status_code == 200, r.text
    yield
    ops().patch("/settings", json={"seller_iban": before.get("seller_iban") or "", "seller_bic": before.get("seller_bic") or ""})


def autopilot(on: bool):
    r = ops().post("/settings/autonomy/autopilot_enabled", json={"enabled": on, "confirmation": "enable" if on else ""})
    assert r.status_code == 200, r.text


@pytest.fixture
def autopilot_on():
    before = ops().get("/settings").json().get("autopilot_enabled")
    autopilot(True)
    yield
    autopilot(bool(before))


@pytest.fixture
def autopilot_off():
    before = ops().get("/settings").json().get("autopilot_enabled")
    autopilot(False)
    yield
    autopilot(bool(before))


def carrier_invoice(*, name: str, iban: str | None, bic: str | None, currency: str, total_cents: int, eur_total_cents: int,
                    reference: str | None, country: str, carrier_id: str | None = None, account: str | None = None,
                    due: date | None = None) -> dict:
    ci_id = str(uuid.uuid4())
    vat = 0
    ci = {"id": ci_id, "supplier": {"carrier_id": carrier_id or str(uuid.uuid4()), "name": name, "business_id": None,
                                    "vat_id": None, "country": country, "iban": iban, "bic": bic},
          "number": f"CI-{uuid.uuid4().hex[:8].upper()}", "invoice_date": (date.today() - timedelta(days=10)).isoformat(),
          "due_date": (due or EXEC + timedelta(days=1)).isoformat(), "currency": currency,
          "net_cents": total_cents - vat, "vat_cents": vat, "total_cents": total_cents,
          "eur": {"net_cents": eur_total_cents, "vat_cents": 0, "total_cents": eur_total_cents},
          "fx": {"rate": None, "rate_date": None, "source": "test"} if currency != "EUR" else {},
          "lines": [], "references": {"shipment_refs": [], "container_nos": [], "bl_nos": [], "booking_nos": []},
          "payment": {"iban": iban, "bic": bic, "reference": reference, "message": None, **({"account": account} if account else {})},
          "source": "upload", "status": "approved", "vat_treatment": "domestic", "test": True, "created_at": iso(now())}
    db.carrier_invoices.insert_one(dict(ci))
    return ci


def payable_for(ci: dict) -> dict:
    r = ops().post("/finance/payables/sync")
    assert r.status_code == 200, r.text
    p = db.payables.find_one({"carrier_invoice_id": ci["id"]}, {"_id": 0})
    assert p, "sync did not create the payable"
    return p


def create_run(payable_ids: list[str], status: int = 201) -> dict:
    r = ops().post("/finance/payment-runs", json={"execution_date": EXEC.isoformat(), "payable_ids": payable_ids})
    assert r.status_code == status, r.text
    return r.json()


def run_file(run_id: str) -> bytes:
    r = ops().get(f"/finance/payment-runs/{run_id}/file")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    return r.content


P1 = "{urn:iso:std:iso:20022:tech:xsd:pain.001.001.09}"
NS1 = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}


# ------------------------------------------------------------------------------------------------ pain.001
def test_payment_run_file_validates_and_sums(autopilot_off):
    fi = fi_ref("1234567890")
    rf = rf_ref(fi_ref("40250012"))
    assert rf_valid(rf)
    a = carrier_invoice(name="Kuljetusliike Testinen Oy", iban="FI4250001510000023", bic="OKOYFIHH", currency="EUR",
                        total_cents=123456, eur_total_cents=123456, reference=" ".join([fi[:5], fi[5:]]), country="FI")
    b = carrier_invoice(name="Spedition Test GmbH", iban="DE89370400440532013000", bic="COBADEFFXXX", currency="EUR",
                        total_cents=98765, eur_total_cents=98765, reference=rf, country="DE")
    c = carrier_invoice(name="Pacific Test Lines Inc", iban=None, bic="CHASUS33", account="000123456789", currency="USD",
                        total_cents=500001, eur_total_cents=431234, reference=None, country="US")
    pa, pb, pc = payable_for(a), payable_for(b), payable_for(c)
    assert pa["status"] == "approved" and pa["amount_cents"] == 123456 and pc["eur_cents"] == 431234
    assert payable_for(a)["id"] == pa["id"]  # idempotent per carrier invoice
    assert db.payables.count_documents({"carrier_invoice_id": a["id"]}) == 1

    run = create_run([pa["id"], pb["id"], pc["id"]])
    assert run["status"] == "pending_approval" and run["xsd_valid"] is True and run["count"] == 3
    assert run["totals"] == {"EUR": 123456 + 98765, "USD": 500001}
    assert run["eur_total_cents"] == 123456 + 98765 + 431234
    card = db.approvals.find_one({"kind": "payment_run", "ref_type": "payment_run", "ref_id": run["id"], "status": "pending"})
    assert card, "Autopilot off: the run must wait on an owner card"
    assert run["number"].startswith(f"PR-{date.today().year}-")

    xml = run_file(run["id"])
    assert_valid(xml, "pain.001.001.09")
    doc = etree.fromstring(xml)
    amounts = [Decimal(x.text) for x in doc.iter(f"{P1}InstdAmt")]
    expected = [Decimal(123456) / 100, Decimal(98765) / 100, Decimal(500001) / 100]
    assert sorted(amounts) == sorted(expected)
    assert doc.findtext("p:CstmrCdtTrfInitn/p:GrpHdr/p:NbOfTxs", namespaces=NS1) == "3"
    assert Decimal(doc.findtext("p:CstmrCdtTrfInitn/p:GrpHdr/p:CtrlSum", namespaces=NS1)) == sum(expected)
    assert doc.findtext("p:CstmrCdtTrfInitn/p:GrpHdr/p:MsgId", namespaces=NS1) == run["msg_id"]
    blocks = doc.findall("p:CstmrCdtTrfInitn/p:PmtInf", NS1)
    assert len(blocks) == 2
    by_cur = {}
    for blk in blocks:
        curs = {x.get("Ccy") for x in blk.iter(f"{P1}InstdAmt")}
        assert len(curs) == 1
        by_cur[curs.pop()] = blk
        txs = blk.findall("p:CdtTrfTxInf", NS1)
        assert blk.findtext("p:NbOfTxs", namespaces=NS1) == str(len(txs))
        assert Decimal(blk.findtext("p:CtrlSum", namespaces=NS1)) == sum(Decimal(t.findtext("p:Amt/p:InstdAmt", namespaces=NS1)) for t in txs)
        assert blk.findtext("p:ReqdExctnDt/p:Dt", namespaces=NS1) == EXEC.isoformat()
        assert blk.findtext("p:DbtrAcct/p:Id/p:IBAN", namespaces=NS1) == DEBTOR_IBAN
        assert blk.findtext("p:DbtrAgt/p:FinInstnId/p:BICFI", namespaces=NS1) == DEBTOR_BIC
    eur, usd = by_cur["EUR"], by_cur["USD"]
    assert eur.findtext("p:PmtTpInf/p:SvcLvl/p:Cd", namespaces=NS1) == "SEPA"
    assert eur.findtext("p:ChrgBr", namespaces=NS1) == "SLEV"
    assert usd.find("p:PmtTpInf/p:SvcLvl", NS1) is None
    assert usd.findtext("p:ChrgBr", namespaces=NS1) == "SHAR"
    usd_tx = usd.find("p:CdtTrfTxInf", NS1)
    assert usd_tx.findtext("p:CdtrAgt/p:FinInstnId/p:BICFI", namespaces=NS1) == "CHASUS33"
    assert usd_tx.findtext("p:CdtrAcct/p:Id/p:Othr/p:Id", namespaces=NS1) == "000123456789"
    assert usd_tx.findtext("p:Cdtr/p:PstlAdr/p:Ctry", namespaces=NS1) == "US"
    assert usd_tx.findtext("p:RmtInf/p:Ustrd", namespaces=NS1) == f"Invoice {c['number']}"

    txs = {t.findtext("p:Cdtr/p:Nm", namespaces=NS1): t for t in eur.findall("p:CdtTrfTxInf", NS1)}
    fi_tx, rf_tx = txs["Kuljetusliike Testinen Oy"], txs["Spedition Test GmbH"]
    # Finnish reference: SCOR, no issuer, digits without spaces (Finance Finland guide 5.1)
    assert fi_tx.findtext("p:RmtInf/p:Strd/p:CdtrRefInf/p:Tp/p:CdOrPrtry/p:Cd", namespaces=NS1) == "SCOR"
    assert fi_tx.find("p:RmtInf/p:Strd/p:CdtrRefInf/p:Tp/p:Issr", NS1) is None
    assert fi_tx.findtext("p:RmtInf/p:Strd/p:CdtrRefInf/p:Ref", namespaces=NS1) == fi
    assert fi_tx.find("p:RmtInf/p:Ustrd", NS1) is None
    # RF reference: SCOR + issuer ISO
    assert rf_tx.findtext("p:RmtInf/p:Strd/p:CdtrRefInf/p:Tp/p:CdOrPrtry/p:Cd", namespaces=NS1) == "SCOR"
    assert rf_tx.findtext("p:RmtInf/p:Strd/p:CdtrRefInf/p:Tp/p:Issr", namespaces=NS1) == "ISO"
    assert rf_tx.findtext("p:RmtInf/p:Strd/p:CdtrRefInf/p:Ref", namespaces=NS1) == rf
    assert rf_tx.findtext("p:CdtrAcct/p:Id/p:IBAN", namespaces=NS1) == "DE89370400440532013000"
    e2e = [t.findtext("p:PmtId/p:EndToEndId", namespaces=NS1) for t in doc.iter(f"{P1}CdtTrfTxInf")]
    assert len(set(e2e)) == 3 and all(len(x) <= 35 for x in e2e)
    stored = {p["id"]: p for p in db.payables.find({"payment_run_id": run["id"]}, {"_id": 0})}
    assert {p["end_to_end_id"] for p in stored.values()} == set(e2e)
    assert all(p["status"] == "scheduled" for p in stored.values())
    assert db.carrier_invoices.find_one({"id": a["id"]})["status"] == "scheduled"

    # owner rejects the card -> run rejected, payables back to approved
    r = ops().post(f"/approvals/{card['id']}/decide", json={"decision": "rejected", "note": "test"})
    assert r.status_code == 200, r.text
    assert ops().get(f"/finance/payment-runs/{run['id']}").json()["status"] == "rejected"
    assert {p["status"] for p in db.payables.find({"id": {"$in": [pa["id"], pb["id"], pc["id"]]}})} == {"approved"}
    db.payables.update_many({"id": {"$in": [pa["id"], pb["id"], pc["id"]]}}, {"$set": {"status": "cancelled"}})


def test_invalid_iban_refused_with_owner_card(autopilot_off):
    bad = "FI4250001510000024"  # last digit changed: mod 97 fails
    assert not iban_ok(bad)
    ci = carrier_invoice(name="Väärä Tili Oy", iban=bad, bic="OKOYFIHH", currency="EUR", total_cents=10000,
                         eur_total_cents=10000, reference=None, country="FI")
    p = payable_for(ci)
    r = ops().post("/finance/payment-runs", json={"execution_date": EXEC.isoformat(), "payable_ids": [p["id"]]})
    assert r.status_code == 400, r.text
    assert "IBAN" in r.json()["detail"] and bad in r.json()["detail"]
    assert db.payables.find_one({"id": p["id"]})["status"] == "approved"
    card = db.approvals.find_one({"kind": "payment_run", "ref_type": "payable", "ref_id": p["id"], "status": "pending"})
    assert card and card["data"]["code"] == "iban"
    db.payables.update_one({"id": p["id"]}, {"$set": {"status": "cancelled"}})


def test_autopilot_on_approves_run_with_audit(autopilot_on):
    ci = carrier_invoice(name="Autopilot Rahti Oy", iban="FI4250001510000023", bic="OKOYFIHH", currency="EUR",
                         total_cents=45600, eur_total_cents=45600, reference=fi_ref("777001"), country="FI")
    p = payable_for(ci)
    run = create_run([p["id"]])
    assert run["status"] == "approved", run.get("decision")
    assert run["decision"]["auto"] is True and run["approved_auto"] is True
    row = db.agent_decisions.find_one({"subject_type": "payment_run", "subject_id": run["id"]})
    assert row and row["action"] == "approve_payment_run" and row["outcome"] == "auto"
    assert db.audit_log.find_one({"entity": "payment_run", "entity_id": run["id"], "action": "payment_run_approved", "data.auto": True})
    assert not db.approvals.find_one({"kind": "payment_run", "ref_id": run["id"]})
    assert run["bank"]["mode"] == "file"
    assert not run["bank"].get("ready_for_upload_at")
    assert not run.get("sent_at")
    assert db.payables.find_one({"id": p["id"]})["status"] == "scheduled"
    assert_valid(run_file(run["id"]), "pain.001.001.09")
    # Approval and downloading a file are not evidence of bank submission.
    stored = db.payment_runs.find_one({"id": run["id"]})
    assert stored["status"] == "approved" and not stored.get("sent_at")
    assert not db.audit_log.find_one({"entity_id": run["id"], "action": "payment_run_sent"})


def test_iban_change_stops_autopilot(autopilot_on):
    carrier = str(uuid.uuid4())
    old_iban, new_iban = "FI4250001510000023", "FI2112345600000785"
    assert iban_ok(old_iban) and iban_ok(new_iban)
    db.payables.insert_one({"id": str(uuid.uuid4()), "carrier_invoice_id": str(uuid.uuid4()), "supplier": {"carrier_id": carrier,
                            "name": "Vaihtuva Tili Oy", "iban": old_iban, "bic": "OKOYFIHH", "country": "FI"},
                            "supplier_key": f"carrier_id:{carrier.upper()}", "currency": "EUR", "amount_cents": 1000,
                            "eur_cents": 1000, "due_date": "2026-01-01", "status": "paid", "paid_at": "2026-01-02T00:00:00Z",
                            "test": True})
    ci = carrier_invoice(name="Vaihtuva Tili Oy", iban=new_iban, bic="NDEAFIHH", currency="EUR", total_cents=777700,
                         eur_total_cents=777700, reference=None, country="FI", carrier_id=carrier)
    p = payable_for(ci)
    run = create_run([p["id"]])
    assert run["status"] == "pending_approval"
    codes = [r["code"] for r in run["decision"]["reasons"]]
    assert "iban_changed" in codes and "switch_off" not in codes
    assert run["iban_changes"][0]["last_paid_iban"] == old_iban and run["iban_changes"][0]["iban"] == new_iban
    card = db.approvals.find_one({"kind": "payment_run", "ref_type": "payment_run", "ref_id": run["id"], "status": "pending"})
    assert card and card["amount_cents"] == 777700
    row = db.agent_decisions.find_one({"subject_id": run["id"]})
    assert row["outcome"] == "owner_queue"
    # the owner approves from the payment-run screen -> card closed, run approved (file ready)
    r = ops().post(f"/finance/payment-runs/{run['id']}/approve", json={"note": "IBAN change confirmed by phone"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert db.approvals.find_one({"id": card["id"]})["status"] == "approved"


# ------------------------------------------------------------------------------------------------ pain.002
def pain002(run: dict, block_id: str, txs: list[tuple[str, str, str | None]], group: str | None = None) -> bytes:
    ns = "urn:iso:std:iso:20022:tech:xsd:pain.002.001.10"
    tx_xml = "".join(
        f"<TxInfAndSts><OrgnlEndToEndId>{e2e}</OrgnlEndToEndId><TxSts>{st}</TxSts>"
        + (f"<StsRsnInf><Rsn><Cd>{code}</Cd></Rsn><AddtlInf>Account closed</AddtlInf></StsRsnInf>" if code else "")
        + "</TxInfAndSts>" for e2e, st, code in txs)
    grp = f"<GrpSts>{group}</GrpSts>" if group else ""
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><Document xmlns="{ns}"><CstmrPmtStsRpt>'
           f"<GrpHdr><MsgId>STS{uuid.uuid4().hex[:12].upper()}</MsgId><CreDtTm>{iso(now())}</CreDtTm></GrpHdr>"
           f"<OrgnlGrpInfAndSts><OrgnlMsgId>{run['msg_id']}</OrgnlMsgId><OrgnlMsgNmId>pain.001.001.09</OrgnlMsgNmId>{grp}"
           f"</OrgnlGrpInfAndSts><OrgnlPmtInfAndSts><OrgnlPmtInfId>{block_id}</OrgnlPmtInfId>{tx_xml}</OrgnlPmtInfAndSts>"
           f"</CstmrPmtStsRpt></Document>")
    return xml.encode()


def test_pain002_rejection_creates_card_and_partial_run(autopilot_off):
    a = carrier_invoice(name="Hylätty Maksu Oy", iban="FI4250001510000023", bic="OKOYFIHH", currency="EUR",
                        total_cents=20000, eur_total_cents=20000, reference=None, country="FI")
    b = carrier_invoice(name="Hyväksytty Maksu Oy", iban="FI2112345600000785", bic="NDEAFIHH", currency="EUR",
                        total_cents=30000, eur_total_cents=30000, reference=None, country="FI")
    pa, pb = payable_for(a), payable_for(b)
    run = create_run([pa["id"], pb["id"]])
    r = ops().post(f"/finance/payment-runs/{run['id']}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    stored = {p["id"]: p for p in db.payables.find({"payment_run_id": run["id"]}, {"_id": 0})}
    e2e_a, e2e_b = stored[pa["id"]]["end_to_end_id"], stored[pb["id"]]["end_to_end_id"]
    block = stored[pa["id"]]["pmt_inf_id"]
    xml = pain002(run, block, [(e2e_a, "RJCT", "AC04"), (e2e_b, "ACCP", None)])
    assert_valid(xml, "pain.002.001.10")

    # a broken report is refused against the official XSD
    bad = xml.replace(b"<TxSts>ACCP</TxSts>", b"<TxSts>ACCEPTED</TxSts>")
    r = ops().post("/finance/bank/pain002", content=bad, headers={"Content-Type": "application/xml"})
    assert r.status_code == 400 and "XSD" in r.json()["detail"]

    r = ops().post("/finance/bank/pain002", content=xml, headers={"Content-Type": "application/xml"})
    assert r.status_code == 200, r.text
    assert r.json()["run"]["status"] == "partially_accepted"
    ra, rb = db.payables.find_one({"id": pa["id"]}), db.payables.find_one({"id": pb["id"]})
    assert ra["status"] == "rejected" and ra["rejection"]["code"] == "AC04"
    assert rb["status"] == "sent"
    card = db.approvals.find_one({"kind": "payment_run", "ref_type": "payable", "ref_id": pa["id"], "status": "pending"})
    assert card and card["data"]["code"] == "AC04" and "AC04" in card["title"]
    assert db.payment_status_reports.find_one({"payment_run_id": run["id"]})["group_status"] is None
    # owner: pay again in the next run
    r = ops().post(f"/approvals/{card['id']}/decide", json={"decision": "approved", "note": "supplier sent new account"})
    assert r.status_code == 200, r.text
    again = db.payables.find_one({"id": pa["id"]})
    assert again["status"] == "approved" and again["payment_run_id"] is None
    db.payables.update_many({"id": {"$in": [pa["id"], pb["id"]]}}, {"$set": {"status": "cancelled"}})


# ------------------------------------------------------------------------------------------------ camt.054
def camt054(entries: list[dict]) -> bytes:
    ns = "urn:iso:std:iso:20022:tech:xsd:camt.054.001.08"
    body = ""
    for e in entries:
        inst = (f"<AmtDtls><InstdAmt><Amt Ccy=\"{e['instd_ccy']}\">{e['instd']}</Amt></InstdAmt></AmtDtls>"
                if e.get("instd") else "")
        body += (f"<Ntry><NtryRef>{uuid.uuid4().hex[:10]}</NtryRef><Amt Ccy=\"{e['ccy']}\">{e['amt']}</Amt>"
                 f"<CdtDbtInd>{e['cd']}</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><BookgDt><Dt>{e['booked']}</Dt></BookgDt>"
                 f"<ValDt><Dt>{e['booked']}</Dt></ValDt><BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>ICDT</Cd>"
                 f"<SubFmlyCd>ESCT</SubFmlyCd></Fmly></Domn></BkTxCd><NtryDtls><TxDtls><Refs>"
                 f"<EndToEndId>{e['e2e']}</EndToEndId></Refs><Amt Ccy=\"{e['ccy']}\">{e['amt']}</Amt>"
                 f"<CdtDbtInd>{e['cd']}</CdtDbtInd>{inst}</TxDtls></NtryDtls></Ntry>")
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><Document xmlns="{ns}"><BkToCstmrDbtCdtNtfctn>'
           f"<GrpHdr><MsgId>CAMT{uuid.uuid4().hex[:12].upper()}</MsgId><CreDtTm>{iso(now())}</CreDtTm></GrpHdr>"
           f"<Ntfctn><Id>N{uuid.uuid4().hex[:10]}</Id><CreDtTm>{iso(now())}</CreDtTm>"
           f"<Acct><Id><IBAN>{DEBTOR_IBAN}</IBAN></Id></Acct>{body}</Ntfctn></BkToCstmrDbtCdtNtfctn></Document>")
    return xml.encode()


def ecb_rate(currency: str, on: str) -> tuple[str, Decimal]:
    doc = db.fx_rates.find_one({"date": {"$lte": on}, f"rates.{currency}": {"$exists": True}}, sort=[("date", -1)])
    assert doc, f"no ECB {currency} rate on or before {on} in fx_rates"
    return doc["date"], Decimal(str(doc["rates"][currency]))


def last_target_day() -> date:
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def test_camt054_debit_pays_and_books_realised_fx(autopilot_off):
    usd = carrier_invoice(name="Ocean Test Carrier Ltd", iban="GB29NWBK60161331926819", bic="NWBKGB2L", currency="USD",
                          total_cents=1234567, eur_total_cents=1100000, reference=None, country="GB")
    usd2 = carrier_invoice(name="Air Test Cargo Ltd", iban="GB29NWBK60161331926819", bic="NWBKGB2L", currency="USD",
                           total_cents=250000, eur_total_cents=220000, reference=None, country="GB")
    eur = carrier_invoice(name="Kotimaan Ajot Oy", iban="FI4250001510000023", bic="OKOYFIHH", currency="EUR",
                          total_cents=40000, eur_total_cents=40000, reference=fi_ref("55501"), country="FI")
    p1, p2, p3 = payable_for(usd), payable_for(usd2), payable_for(eur)
    run = create_run([p1["id"], p2["id"], p3["id"]])
    assert ops().post(f"/finance/payment-runs/{run['id']}/approve", json={}).json()["status"] == "approved"
    assert ops().post(f"/finance/payment-runs/{run['id']}/send", json={"uploaded": True}).json()["status"] == "sent"
    stored = {p["id"]: p for p in db.payables.find({"payment_run_id": run["id"]}, {"_id": 0})}
    booked = last_target_day().isoformat()
    xml = camt054([
        # USD account debit, no EUR amount from the bank: settled EUR = ECB reference rate of the booking date
        {"e2e": stored[p1["id"]]["end_to_end_id"], "amt": "12345.67", "ccy": "USD", "cd": "DBIT", "booked": booked},
        # EUR account debited 2,198.40 for USD 2,500.00 (bank's own rate)
        {"e2e": stored[p2["id"]]["end_to_end_id"], "amt": "2198.40", "ccy": "EUR", "cd": "DBIT", "booked": booked,
         "instd": "2500.00", "instd_ccy": "USD"},
        {"e2e": stored[p3["id"]]["end_to_end_id"], "amt": "400.00", "ccy": "EUR", "cd": "DBIT", "booked": booked},
        # incoming customer payment: s2's reconciliation, ignored here
        {"e2e": "CUSTOMER-PAYMENT-1", "amt": "999.00", "ccy": "EUR", "cd": "CRDT", "booked": booked},
    ])
    assert_valid(xml, "camt.054.001.08")
    r = ops().post("/finance/bank/camt054-debits", files={"file": ("camt054.xml", xml, "application/xml")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 3 and body["ignored_credit_entries"] == 1

    rate_date, rate = ecb_rate("USD", booked)
    settled = int((Decimal(1234567) / rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    q1 = db.payables.find_one({"id": p1["id"]})
    assert q1["status"] == "paid" and q1["paid_at"].startswith(booked)
    assert q1["settled_eur_cents"] == settled
    assert q1["realised_fx_cents"] == 1100000 - settled  # AP: paying fewer EUR than booked is a gain
    assert q1["bank_debit"]["fx"]["rate_date"] == rate_date
    q2 = db.payables.find_one({"id": p2["id"]})
    assert q2["status"] == "paid" and q2["settled_eur_cents"] == 219840 and q2["realised_fx_cents"] == 220000 - 219840
    q3 = db.payables.find_one({"id": p3["id"]})
    assert q3["status"] == "paid" and q3["realised_fx_cents"] is None
    assert db.carrier_invoices.find_one({"id": usd["id"]})["status"] == "paid"
    assert ops().get(f"/finance/payment-runs/{run['id']}").json()["status"] == "accepted"

    # the same file again changes nothing (idempotent)
    r = ops().post("/finance/bank/camt054-debits", content=xml, headers={"Content-Type": "application/xml"})
    assert r.status_code == 200
    assert db.payables.find_one({"id": p1["id"]})["realised_fx_cents"] == 1100000 - settled

    # payables list: filters, counts and totals per currency + EUR
    r = ops().get("/finance/payables", params={"payment_run_id": run["id"], "currency": "USD"})
    lst = r.json()
    assert lst["total"] == 2 and lst["totals"] == {"USD": 1234567 + 250000} and lst["eur_total_cents"] == 1100000 + 220000
    assert lst["counts"].get("paid", 0) >= 3


def test_writes_need_billing_write():
    from test_v4_support import customer
    r = customer().post("/finance/payment-runs", json={})
    assert r.status_code in (401, 403)
    r = customer().get("/finance/payables")
    assert r.status_code in (401, 403)


def test_credit_note_reduces_payable_and_unknown_bank_reference_is_rejected(autopilot_off):
    ci = carrier_invoice(name="Credit adjustment test", iban="FI4250001510000023", bic="OKOYFIHH", currency="EUR",
                         total_cents=125500, eur_total_cents=125500, reference=None, country="FI")
    db.carrier_invoices.update_one({"id": ci["id"]}, {"$set": {"amount_due_cents": 100400}})
    p = payable_for(ci)
    assert p["amount_cents"] == 100400 and p["eur_cents"] == 100400
    run = create_run([p["id"]])
    assert ops().post(f"/finance/payment-runs/{run['id']}/approve").status_code == 200
    p = db.payables.find_one({"id": p["id"]})
    xml = pain002(run, p["pmt_inf_id"], [("NOT-OUR-PAYMENT", "ACSC", None)], group="ACSC")
    assert_valid(xml, "pain.002.001.10")
    response = ops().post("/finance/bank/pain002", content=xml, headers={"Content-Type": "application/xml"})
    assert response.status_code == 400, response.text
    assert db.payables.find_one({"id": p["id"]})["status"] == "scheduled"
    assert db.payment_runs.find_one({"id": run["id"]})["status"] == "approved"
