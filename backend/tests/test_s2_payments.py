"""s2 payments: durable Stripe webhook, camt.054 bank notifications matched by reference, owner-queue cards, dunning
stages and the daily reconciliation report. In-process against a throw-away MongoDB database; the real Stripe test
runs only when HUOLITSIJA_STRIPE_API_KEY is in the environment (restricted test key)."""
import os
import uuid

os.environ["DB_NAME"] = f"huo_s2_pay_{uuid.uuid4().hex[:8]}"
os.environ["EMAIL_PROVIDER"] = "log"  # never deliver test e-mail, whatever the shell has
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import timedelta  # noqa: E402
from decimal import ROUND_HALF_UP, Decimal  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_LOOP = asyncio.new_event_loop()


def run(coro):
    """Run on the loop the Motor client is bound to: ours, or the default loop when another test module imported
    core.db first (Motor binds to the loop that is current when the client is created)."""
    async def _bind():
        from core.db import client
        return client
    if "core.db" not in sys.modules:
        _LOOP.run_until_complete(_bind())
    from core.db import client
    return client.get_io_loop().run_until_complete(coro)


import pytest  # noqa: E402

from core import config  # noqa: E402
from core.util import iso, now  # noqa: E402

if config.DB_NAME != os.environ["DB_NAME"]:  # core.config was imported by another test module first: never touch its DB
    pytest.skip("run test_s2_payments.py in its own pytest process", allow_module_level=True)

IBAN = "FI2112345600000785"
WHSEC = "whsec_s2_" + uuid.uuid4().hex
OPS = {"id": "ops-s2", "name": "Ops S2", "email": "ops-s2@huolitsija.example", "org_type": "internal", "role": "ops_admin",
       "permissions": ["billing.write", "customers.write", "settings.write"]}
VIEWER = {"id": "viewer-s2", "name": "Viewer S2", "org_type": "internal", "role": "ops_agent", "permissions": []}
S: dict = {}


# --------------------------------------------------------------------------------------------- fixtures
def setup_module(_m):
    async def go():
        from core.db import db, ensure_indexes
        from core.settings import update_settings
        import services.agents  # noqa: F401  (registers jobs, schedules and event handlers like the server does)
        await ensure_indexes()
        await update_settings({"autopilot_enabled": True, "autopilot_halted": False, "seller_iban": IBAN,
                               "seller_bic": "NDEAFIHH"})
        cid = str(uuid.uuid4())
        S["company"] = {"id": cid, "name": "Testi Kuljetus Oy", "business_id": "3990009-9", "vat_id": "FI39900099",
                        "country": "FI", "address": {"street": "Satamakatu 1", "postal_code": "00100", "city": "Helsinki",
                                                     "country": "FI"},
                        "kyc_status": "approved", "payment_terms_days": 14, "billing_email": "laskut@testikuljetus.example",
                        "status": "active", "created_at": "2026-01-01T00:00:00Z"}
        await db.raw.companies.insert_one(dict(S["company"]))
        await db.raw.users.insert_one({"id": str(uuid.uuid4()), "email": "admin@testikuljetus.example", "name": "Asiakas Admin",
                                       "role": "customer_admin", "org_type": "customer", "company_id": cid, "locale": "fi"})
        await db.raw.users.insert_one({"id": "owner-s2", "email": "owner@huolitsija.example", "name": "Omistaja",
                                       "role": "ops_admin", "org_type": "internal", "locale": "fi", "notify_email": True})
    try:
        run(go())
    except BaseException:
        teardown_module(_m)  # pytest skips teardown_module when setup fails: do not leave the database behind
        raise


def teardown_module(_m):
    from core.db import client
    run(client.drop_database(os.environ["DB_NAME"]))
    if client.get_io_loop() is _LOOP:
        _LOOP.close()


async def _invoice(amount_cents: int, *, due_days_ago: int | None = None, issue: bool = True) -> dict:
    from services import billing
    inv = await billing.create_manual_invoice({"company_id": S["company"]["id"], "lines": [
        {"description": "Huolintapalkkio", "qty": 1, "unit_price_cents": amount_cents, "vat_code": "FI_25_5"}]}, OPS)
    if not issue:
        return inv
    issue_date = billing.today_fi() - timedelta(days=due_days_ago + 14) if due_days_ago is not None else None
    return await billing.issue_invoice(inv["id"], OPS, send=False, issue_date=issue_date)


async def _work(*kinds) -> int:
    from core.jobs import work_once
    return await work_once("test-s2", max_jobs=50, kinds=list(kinds))


async def _inv(invoice_id: str) -> dict:
    from core.db import db
    return await db.raw.invoices.find_one({"id": invoice_id}, {"_id": 0})


def _yesterday() -> str:
    from services import billing
    return (billing.today_fi() - timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------------------------- camt.054 fixture
def _amt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def camt054(entries: list[dict], *, version: str = "02", msg_id: str | None = None, ntf_id: str | None = None,
            created: str = "2026-10-24T06:15:02+03:00", namespace: bool = True) -> bytes:
    """A bank's camt.054 debit/credit notification. v02 layout (Sts text, AmtDtls/TxAmt, Dbtr/Nm) or v08 layout
    (Sts/Cd, TxDtls/Amt, Dbtr/Pty/Nm)."""
    v8 = int(version) >= 8
    msg_id = msg_id or f"CAMT054-{uuid.uuid4().hex[:12].upper()}"
    ntf_id = ntf_id or f"NTF{uuid.uuid4().hex[:10].upper()}"
    ntries = []
    for i, e in enumerate(entries, 1):
        txs = []
        for t in e.get("txs", []):
            rmt = ""
            if t.get("ref"):
                tp = f"<Tp><CdOrPrtry><Cd>{t['ref_type']}</Cd></CdOrPrtry></Tp>" if t.get("ref_type") else ""
                rmt += f"<Strd><CdtrRefInf>{tp}<Ref>{t['ref']}</Ref></CdtrRefInf></Strd>"
            if t.get("ustrd"):
                rmt = f"<Ustrd>{t['ustrd']}</Ustrd>" + rmt
            name = (f"<Dbtr><Pty><Nm>{t['debtor']}</Nm></Pty></Dbtr>" if v8 else f"<Dbtr><Nm>{t['debtor']}</Nm></Dbtr>") \
                if t.get("debtor") else ""
            amount = (f"<Amt Ccy=\"{t.get('ccy', 'EUR')}\">{_amt(t['amount'])}</Amt><CdtDbtInd>{e['cd']}</CdtDbtInd>" if v8 else
                      f"<AmtDtls><TxAmt><Amt Ccy=\"{t.get('ccy', 'EUR')}\">{_amt(t['amount'])}</Amt></TxAmt></AmtDtls>")
            txs.append(f"""
          <TxDtls>
            <Refs><AcctSvcrRef>{t['archive']}</AcctSvcrRef><EndToEndId>NOTPROVIDED</EndToEndId></Refs>
            {amount}
            <RltdPties>{name}<DbtrAcct><Id><IBAN>FI4950009420028730</IBAN></Id></DbtrAcct></RltdPties>
            {f'<RmtInf>{rmt}</RmtInf>' if rmt else ''}
            <RltdDts><AccptncDtTm>{e['booking']}T09:12:00+03:00</AccptncDtTm></RltdDts>
          </TxDtls>""")
        btch = f"<Btch><NbOfTxs>{len(txs)}</NbOfTxs></Btch>" if len(txs) > 1 else ""
        sts = "<Sts><Cd>BOOK</Cd></Sts>" if v8 else "<Sts>BOOK</Sts>"
        bk = ("<BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>RCDT</Cd><SubFmlyCd>DMCT</SubFmlyCd></Fmly></Domn>"
              "<Prtry><Cd>705</Cd><Issr>FFFS</Issr></Prtry></BkTxCd>" if e["cd"] == "CRDT" else
              "<BkTxCd><Domn><Cd>ACMT</Cd><Fmly><Cd>MDOP</Cd><SubFmlyCd>CHRG</SubFmlyCd></Fmly></Domn></BkTxCd>")
        dtls = f"<NtryDtls>{btch}{''.join(txs)}\n        </NtryDtls>" if txs else ""
        ntries.append(f"""
      <Ntry>
        <NtryRef>{i}</NtryRef>
        <Amt Ccy="{e.get('ccy', 'EUR')}">{_amt(e['amount'])}</Amt>
        <CdtDbtInd>{e['cd']}</CdtDbtInd>
        {sts}
        <BookgDt><Dt>{e['booking']}</Dt></BookgDt>
        <ValDt><Dt>{e['booking']}</Dt></ValDt>
        <AcctSvcrRef>{e['archive']}</AcctSvcrRef>
        {bk}
        {dtls}
      </Ntry>""")
    xmlns = f' xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.{version}"' if namespace else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document{xmlns} xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <BkToCstmrDbtCdtNtfctn>
    <GrpHdr>
      <MsgId>{msg_id}</MsgId>
      <CreDtTm>{created}</CreDtTm>
      <MsgRcpt><Nm>HUOLITSIJA OY</Nm></MsgRcpt>
    </GrpHdr>
    <Ntfctn>
      <Id>{ntf_id}</Id>
      <CreDtTm>{created}</CreDtTm>
      <Acct>
        <Id><IBAN>{IBAN}</IBAN></Id>
        <Ccy>EUR</Ccy>
        <Svcr><FinInstnId><BIC>NDEAFIHH</BIC></FinInstnId></Svcr>
      </Acct>{''.join(ntries)}
    </Ntfctn>
  </BkToCstmrDbtCdtNtfctn>
</Document>
""".encode()


def _archive() -> str:
    return "2610" + uuid.uuid4().hex[:14].upper()


def main_fixture(inv_fi: dict, inv_rf: dict, booking: str) -> tuple[bytes, dict]:
    """One batch credit entry with two reference payments (Finnish reference with leading zeros, RF in print groups),
    one credit with an unknown reference and one debit (bank service fee)."""
    rf = inv_rf["rf_reference"]
    arch = {"fi": _archive(), "rf": _archive(), "unknown": _archive(), "fee": _archive(), "batch": _archive()}
    entries = [
        {"cd": "CRDT", "amount": inv_fi["total_cents"] + inv_rf["total_cents"], "booking": booking, "archive": arch["batch"],
         "txs": [{"amount": inv_fi["total_cents"], "archive": arch["fi"], "debtor": "TESTI KULJETUS OY",
                  "ref": "0000" + inv_fi["reference"].replace(" ", "")},
                 {"amount": inv_rf["total_cents"], "archive": arch["rf"], "debtor": "Testi Kuljetus Oy", "ref_type": "SCOR",
                  "ref": " ".join(rf[i:i + 4] for i in range(0, len(rf), 4))}]},
        {"cd": "CRDT", "amount": 12345, "booking": booking, "archive": arch["unknown"],
         "txs": [{"amount": 12345, "archive": arch["unknown"], "debtor": "Tuntematon Maksaja Ky", "ref": "1234561",
                  "ustrd": "Rahti lokakuu"}]},
        {"cd": "DBIT", "amount": 590, "booking": booking, "archive": arch["fee"], "txs": []},
    ]
    return camt054(entries), arch


# --------------------------------------------------------------------------------------------- reconciliation
def test_reconciliation_report_ok_then_attention_then_ok():
    async def go():
        from core.db import db
        from routes.admin import DecisionIn, decide as decide_route
        from services import billing, payments, reconciliation
        inv = await _invoice(20000)
        await reconciliation.run_reconciliation()
        rep = await db.raw.owner_reports.find_one({"kind": "reconciliation"}, {"_id": 0}, sort=[("created_at", -1)])
        assert rep["status"] == "ok" and rep["data"]["counts"] == {}
        assert rep["data"]["totals"]["open_cents"] == inv["total_cents"] and rep["data"]["totals"]["open_count"] == 1
        if payments.stripe_enabled():  # CI may run with a Stripe test key: the check runs against Stripe test mode
            st = rep["data"]["stripe"]
            assert st["checked"] is True and st["mode"] == "test" and st["errors"] == [] and st["recovered"] == [], st
            stripe_line = "maksusivua käyty läpi"
        else:
            assert rep["data"]["stripe"] == {"checked": False, "skipped": "stripe_not_configured"}
            stripe_line = "Stripe-avainta ei ole asetettu"
        assert not await db.raw.alerts.find_one({"key": "reconciliation", "status": "open"})
        day = billing.today_fi().isoformat()
        mail = await db.raw.email_outbox.find_one({"to": "owner@huolitsija.example", "subject": f"Täsmäytys {day}: kunnossa"},
                                                  {"_id": 0}, sort=[("created_at", -1)])
        assert mail and "Avoimet saatavat" in mail["text"] and stripe_line in mail["text"], mail

        data = camt054([
            {"cd": "CRDT", "amount": inv["total_cents"], "booking": _yesterday(), "archive": (a1 := _archive()),
             "txs": [{"amount": inv["total_cents"], "archive": a1, "debtor": "Testi Kuljetus Oy", "ref": inv["reference"]}]},
            {"cd": "CRDT", "amount": 4321, "booking": _yesterday(), "archive": (a2 := _archive()),
             "txs": [{"amount": 4321, "archive": a2, "debtor": "Joku Muu Oy", "ref": "RF18539007547034", "ref_type": "SCOR"}]}])
        await reconciliation.store_statement(data, "recon.xml", "upload", OPS)
        await _work("payments.camt054_import")
        assert (await _inv(inv["id"]))["status"] == "paid"
        out = await reconciliation.run_reconciliation()
        rep = await db.raw.owner_reports.find_one({"id": out["report_id"]}, {"_id": 0})
        assert rep["status"] == "attention" and rep["data"]["counts"] == {"bank_unmatched": 1}
        assert rep["data"]["totals"]["received_yesterday"] == {"bank_transfer": inv["total_cents"]}
        assert rep["data"]["totals"]["open_cents"] == 0
        alert = await db.raw.alerts.find_one({"key": "reconciliation", "status": "open"}, {"_id": 0})
        assert alert and alert["severity"] == "warning" and alert["data"]["report_id"] == rep["id"]
        mail = await db.raw.email_outbox.find_one({"to": "owner@huolitsija.example", "subject": f"Täsmäytys {day}: 1 poikkeamaa"},
                                                  {"_id": 0})
        assert mail and "1 kohdistamatonta pankkimaksua" in mail["text"]

        tx = await db.raw.bank_transactions.find_one({"archive_ref": a2}, {"_id": 0})
        await decide_route(tx["approval_id"], DecisionIn(decision="rejected", note="Ei meidän maksu"), OPS)
        assert (await db.raw.bank_transactions.find_one({"archive_ref": a2}))["status"] == "ignored"
        out = await reconciliation.run_reconciliation()
        assert out["status"] == "ok"
        assert not await db.raw.alerts.find_one({"key": "reconciliation", "status": "open"})
        assert await db.raw.alerts.find_one({"key": "reconciliation", "status": "resolved"})
    run(go())


# --------------------------------------------------------------------------------------------- camt.054 parsing
def test_parse_camt054_v02_v08_and_rejects_other_files():
    from services.reconciliation import parse_camt054
    inv_fi = {"total_cents": 19829, "reference": "2 02600 01230"}
    inv_rf = {"total_cents": 5021, "rf_reference": "RF91998877662"}
    data, arch = main_fixture(inv_fi, inv_rf, "2026-10-23")
    p = parse_camt054(data)
    assert p["version"] == "camt.054.001.02" and p["iban"] == IBAN and p["created_at"] == "2026-10-24T06:15:02+03:00"
    assert p["statement_id"].startswith("NTF") and len(p["entries"]) == 4
    fi, rf, unknown, fee = p["entries"]
    assert (fi["amount_cents"], fi["currency"], fi["credit_debit"], fi["status"]) == (19829, "EUR", "credit", "BOOK")
    assert fi["archive_ref"] == arch["fi"] and fi["entry_ref"] == arch["batch"] and fi["booking_date"] == "2026-10-23"
    assert fi["reference"] == "000020260001230" and fi["debtor"] == "TESTI KULJETUS OY" and fi["end_to_end_id"] is None
    assert rf["reference"] == "RF91 9988 7766 2" and rf["reference_type"] == "SCOR" and rf["amount_cents"] == 5021
    assert unknown["text"] == "Rahti lokakuu" and unknown["archive_ref"] == arch["unknown"]
    assert fee["credit_debit"] == "debit" and fee["amount_cents"] == 590 and fee["archive_ref"] == arch["fee"]

    v8 = camt054([{"cd": "CRDT", "amount": 158000, "booking": "2026-10-22", "archive": "A1",
                   "txs": [{"amount": 100000, "archive": "T1", "debtor": "Oy Vienti Ab", "ref": "RF3620260001230"},
                           {"amount": 58000, "archive": "T2", "debtor": "Tuonti Oy", "ref": "20260001230"}]}], version="08")
    p8 = parse_camt054(v8)
    assert p8["version"] == "camt.054.001.08"
    assert [(e["amount_cents"], e["debtor"], e["status"], e["archive_ref"]) for e in p8["entries"]] == [
        (100000, "Oy Vienti Ab", "BOOK", "T1"), (58000, "Tuonti Oy", "BOOK", "T2")]
    plain = parse_camt054(camt054([{"cd": "CRDT", "amount": 100, "booking": "2026-10-22", "archive": "X9", "txs": []}],
                                  namespace=False))
    assert plain["version"] is None and plain["entries"][0]["archive_ref"] == "X9" and plain["entries"][0]["amount_cents"] == 100

    with pytest.raises(ValueError, match="not valid XML"):
        parse_camt054(b"IBAN;Summa;Viite\nFI21;10,00;123")
    with pytest.raises(ValueError, match="empty"):
        parse_camt054(b"  ")
    with pytest.raises(ValueError, match="camt.053"):
        parse_camt054(b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt/></Document>')
    with pytest.raises(ValueError, match="pain.001"):
        parse_camt054(b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"><CstmrCdtTrfInitn/></Document>')
    with pytest.raises(ValueError, match="not supported"):
        parse_camt054(data.replace(b"camt.054.001.02", b"camt.054.001.01"))
    with pytest.raises(ValueError, match="DTD"):
        parse_camt054(b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "y">]><Document><BkToCstmrDbtCdtNtfctn/></Document>')
    with pytest.raises(ValueError, match="BkToCstmrDbtCdtNtfctn"):
        parse_camt054(b"<invoice><total>10</total></invoice>")


# --------------------------------------------------------------------------------------------- import + matching
def test_import_matches_by_reference_once_and_unknown_goes_to_owner():
    async def go():
        from core.db import db
        from services import reconciliation
        inv_fi, inv_rf = await _invoice(15800), await _invoice(4000)
        booking = _yesterday()
        data, arch = main_fixture(inv_fi, inv_rf, booking)
        st = await reconciliation.store_statement(data, "tiliote-1.xml", "upload", OPS)
        assert st["status"] == "queued" and st["entries_count"] == 4 and st["iban"] == IBAN and st["source"] == "upload"
        again = await reconciliation.store_statement(data, "tiliote-1.xml", "upload", OPS)
        assert again["duplicate"] is True and again["id"] == st["id"]
        assert await _work("payments.camt054_import") == 1
        st = await db.raw.bank_statements.find_one({"id": st["id"]}, {"_id": 0})
        assert (st["status"], st["matched"], st["unmatched"], st["ignored"]) == ("imported", 2, 1, 1)

        for inv, a in ((inv_fi, arch["fi"]), (inv_rf, arch["rf"])):
            cur = await _inv(inv["id"])
            assert cur["status"] == "paid" and cur["paid_cents"] == inv["total_cents"]
            assert [(p["method"], p["ref"], p["amount_cents"], p["paid_at"]) for p in cur["payments"]] == [
                ("bank_transfer", a, inv["total_cents"], booking)]
            assert cur["payment"]["method"] == "bank_transfer" and cur["payments"][0]["recorded_by"]["id"] == "agent:payments_agent"
            tx = await db.raw.bank_transactions.find_one({"archive_ref": a}, {"_id": 0})
            assert tx["status"] == "matched" and tx["invoice_id"] == inv["id"] and tx["decision_id"]
            dec = await db.raw.agent_decisions.find_one({"id": tx["decision_id"]}, {"_id": 0})
            assert dec["agent"] == "payments_agent" and dec["action"] == "match_bank_payment" and dec["outcome"] == "auto"
            assert dec["confidence"] == 1.0
            assert await db.raw.email_outbox.count_documents({"tag": "invoice_paid", "ref.id": inv["id"]}) == 1
        assert (await db.raw.bank_transactions.find_one({"archive_ref": arch["fi"]}))["reference"] == inv_fi["reference"].replace(" ", "")

        fee = await db.raw.bank_transactions.find_one({"archive_ref": arch["fee"]}, {"_id": 0})
        assert fee["status"] == "ignored" and fee["ignore_reason"] == "debit" and fee["credit_debit"] == "debit"
        unk = await db.raw.bank_transactions.find_one({"archive_ref": arch["unknown"]}, {"_id": 0})
        assert unk["status"] == "unmatched" and unk["approval_id"] and unk["reference"] == "1234561"
        card = await db.raw.approvals.find_one({"id": unk["approval_id"]}, {"_id": 0})
        assert card["kind"] == "payment_match" and card["status"] == "pending"
        assert card["data"]["transaction_id"] == unk["id"] and [r["code"] for r in card["reasons"]] == ["no_match"]
        assert card["title_fi"].startswith("Kohdistamaton pankkimaksu") and "Tuntematon Maksaja Ky" in card["title_fi"]
        S["unknown_tx"] = unk["id"]

        # The same entries in another file (bank re-sent the notification with a new message id): booked once.
        data2 = data.replace(b"<MsgId>", b"<MsgId>RESENT-")
        st2 = await reconciliation.store_statement(data2, "tiliote-1b.xml", "webhook", None)
        assert st2["id"] != st["id"] and not st2.get("duplicate")
        await _work("payments.camt054_import")
        st2 = await db.raw.bank_statements.find_one({"id": st2["id"]}, {"_id": 0})
        assert st2["status"] == "imported" and st2["matched"] == 2
        assert len((await _inv(inv_fi["id"]))["payments"]) == 1 and len((await _inv(inv_rf["id"]))["payments"]) == 1
        assert await db.raw.bank_transactions.count_documents({"archive_ref": {"$in": list(arch.values())}}) == 4
        assert await db.raw.approvals.count_documents({"kind": "payment_match", "data.transaction_id": unk["id"]}) == 1
    run(go())


def test_approving_the_card_with_an_invoice_records_the_payment():
    async def go():
        from core.db import db
        from routes.admin import DecisionIn, decide as decide_route
        target = await _invoice(9837)  # 9837 + 25.5 % VAT = 12345 cents, the unknown payment's amount
        assert target["total_cents"] == 12345
        tx = await db.raw.bank_transactions.find_one({"id": S["unknown_tx"]}, {"_id": 0})
        viewer = await decide_route_403(tx["approval_id"])
        assert viewer == 403
        await decide_route(tx["approval_id"], DecisionIn(decision="approved", note="Asiakas vahvisti",
                                                         data={"invoice_id": target["id"]}), OPS)
        cur = await _inv(target["id"])
        assert cur["status"] == "paid" and [(p["method"], p["ref"], p["amount_cents"]) for p in cur["payments"]] == [
            ("bank_transfer", tx["archive_ref"], 12345)]
        assert cur["payments"][0]["recorded_by"]["id"] == OPS["id"] and cur["payments"][0]["approval_id"] == tx["approval_id"]
        tx = await db.raw.bank_transactions.find_one({"id": S["unknown_tx"]}, {"_id": 0})
        assert tx["status"] == "matched" and tx["invoice_id"] == target["id"]
    run(go())


async def decide_route_403(approval_id: str) -> int:
    from fastapi import HTTPException

    from routes.admin import DecisionIn, decide as decide_route
    try:
        await decide_route(approval_id, DecisionIn(decision="approved", data={}), VIEWER)
    except HTTPException as exc:
        return exc.status_code
    return 200


def test_autopilot_off_sends_reference_matches_to_the_owner():
    async def go():
        from core.db import db
        from core.settings import update_settings
        from routes.admin import DecisionIn, decide as decide_route
        from services import reconciliation
        await update_settings({"autopilot_enabled": False})
        try:
            inv = await _invoice(30000)
            a = _archive()
            data = camt054([{"cd": "CRDT", "amount": inv["total_cents"], "booking": _yesterday(), "archive": a,
                             "txs": [{"amount": inv["total_cents"], "archive": a, "debtor": "Testi Kuljetus Oy",
                                      "ref": inv["reference"]}]}])
            await reconciliation.store_statement(data, "off.xml", "upload", OPS)
            await _work("payments.camt054_import")
            cur = await _inv(inv["id"])
            assert cur["status"] == "open" and cur["paid_cents"] == 0
            tx = await db.raw.bank_transactions.find_one({"archive_ref": a}, {"_id": 0})
            assert tx["status"] == "unmatched" and tx["match_kind"] == "exact" and tx["candidate_invoice_ids"] == [inv["id"]]
            card = await db.raw.approvals.find_one({"id": tx["approval_id"]}, {"_id": 0})
            assert card["kind"] == "payment_match" and [r["code"] for r in card["reasons"]] == ["autopilot_off"]
            assert card["data"]["candidate_invoice_ids"] == [inv["id"]] and card["title_fi"].startswith("Pankkimaksu laskulle")
            dec = await db.raw.agent_decisions.find_one({"id": tx["decision_id"]}, {"_id": 0})
            assert dec["outcome"] == "owner_queue" and dec["confidence"] == 1.0
            # Approved without choosing: the single candidate is booked.
            await decide_route(card["id"], DecisionIn(decision="approved"), OPS)
            cur = await _inv(inv["id"])
            assert cur["status"] == "paid" and cur["payments"][0]["ref"] == a
        finally:
            await update_settings({"autopilot_enabled": True})
    run(go())


def test_partial_payment_is_booked_and_overpayment_needs_the_owner():
    async def go():
        from core.db import db
        from routes.admin import DecisionIn, decide as decide_route
        from services import reconciliation
        from services import billing
        part, over, usd = await _invoice(10000), await _invoice(8000), await _invoice(6000)
        cn = await billing.credit_invoice((await _invoice(3000))["id"], reason="Virheellinen lasku", line_ids=None, actor=OPS)
        a1, a2, a3, a4 = _archive(), _archive(), _archive(), _archive()
        data = camt054([
            {"cd": "CRDT", "amount": 3765, "booking": _yesterday(), "archive": a4,
             "txs": [{"amount": 3765, "archive": a4, "debtor": "Testi Kuljetus Oy", "ref": cn["reference"]}]},
            {"cd": "CRDT", "amount": usd["total_cents"], "ccy": "USD", "booking": _yesterday(), "archive": a3,
             "txs": [{"amount": usd["total_cents"], "ccy": "USD", "archive": a3, "debtor": "Testi Kuljetus Oy",
                      "ref": usd["reference"]}]},
            {"cd": "CRDT", "amount": 5000, "booking": _yesterday(), "archive": a1,
             "txs": [{"amount": 5000, "archive": a1, "debtor": "Testi Kuljetus Oy", "ref": part["reference"]}]},
            {"cd": "CRDT", "amount": over["total_cents"] + 1000, "booking": _yesterday(), "archive": a2,
             "txs": [{"amount": over["total_cents"] + 1000, "archive": a2, "debtor": "Testi Kuljetus Oy",
                      "ref": over["rf_reference"]}]}])
        await reconciliation.store_statement(data, "partial.xml", "upload", OPS)
        await _work("payments.camt054_import")
        cur = await _inv(part["id"])
        assert cur["status"] == "open" and cur["paid_cents"] == 5000
        t1 = await db.raw.bank_transactions.find_one({"archive_ref": a1}, {"_id": 0})
        assert t1["status"] == "partial"
        assert (await db.raw.agent_decisions.find_one({"id": t1["decision_id"]}))["confidence"] == 0.9
        t2 = await db.raw.bank_transactions.find_one({"archive_ref": a2}, {"_id": 0})
        assert t2["status"] == "unmatched" and (await _inv(over["id"]))["paid_cents"] == 0
        card = await db.raw.approvals.find_one({"id": t2["approval_id"]}, {"_id": 0})
        assert [r["code"] for r in card["reasons"]] == ["overpaid"] and card["data"]["candidate_invoice_ids"][0] == over["id"]
        await decide_route(card["id"], DecisionIn(decision="approved", data={"invoice_id": over["id"]}), OPS)
        assert (await _inv(over["id"]))["paid_cents"] == over["total_cents"] + 1000
        t4 = await db.raw.bank_transactions.find_one({"archive_ref": a4}, {"_id": 0})
        card4 = await db.raw.approvals.find_one({"id": t4["approval_id"]}, {"_id": 0})
        assert t4["status"] == "unmatched" and [r["code"] for r in card4["reasons"]] == ["credit_note"]
        assert cn["id"] not in card4["data"]["candidate_invoice_ids"] and (await _inv(cn["id"]))["paid_cents"] == 0
        # A payment in another currency is never booked by the agent; approving it needs the euro amount.
        t3 = await db.raw.bank_transactions.find_one({"archive_ref": a3}, {"_id": 0})
        assert t3["status"] == "unmatched" and t3["currency"] == "USD" and (await _inv(usd["id"]))["paid_cents"] == 0
        card3 = await db.raw.approvals.find_one({"id": t3["approval_id"]}, {"_id": 0})
        assert [r["code"] for r in card3["reasons"]] == ["currency"]
        await decide_route(card3["id"], DecisionIn(decision="approved"), OPS)
        t3 = await db.raw.bank_transactions.find_one({"archive_ref": a3}, {"_id": 0})
        assert t3["status"] == "unmatched" and t3["approval_id"] != card3["id"]
        card3b = await db.raw.approvals.find_one({"id": t3["approval_id"]}, {"_id": 0})
        assert card3b["status"] == "pending" and "eur_amount_needed" in [r["code"] for r in card3b["reasons"]]
        assert (await _inv(usd["id"]))["paid_cents"] == 0
        await decide_route(card3b["id"], DecisionIn(decision="approved", data={"invoice_id": usd["id"], "amount_cents": 6900}), OPS)
        cur = await _inv(usd["id"])
        assert cur["paid_cents"] == 6900 and cur["payments"][0]["ref"] == a3
        assert (await db.raw.bank_transactions.find_one({"archive_ref": a3}))["status"] == "partial"
        out = await reconciliation.run_reconciliation()
        assert out["status"] == "attention" and out["counts"].get("overpaid") == 1 and out["counts"].get("bank_partial") == 2
    run(go())


# --------------------------------------------------------------------------------------------- Stripe webhook
def _sign(payload: bytes, secret: str = WHSEC, ts: int | None = None) -> str:
    t = ts or int(time.time())
    return f"t={t},v1={hmac.new(secret.encode(), f'{t}.{payload.decode()}'.encode(), hashlib.sha256).hexdigest()}"


def _pi_event(inv: dict, event_id: str | None = None) -> bytes:
    pi = f"pi_s2_{uuid.uuid4().hex[:16]}"
    return json.dumps({"id": event_id or f"evt_s2_{uuid.uuid4().hex[:16]}", "object": "event", "type": "payment_intent.succeeded",
                       "api_version": "2024-06-20", "created": int(time.time()), "livemode": False,
                       "data": {"object": {"id": pi, "object": "payment_intent", "amount": inv["total_cents"],
                                           "amount_received": inv["total_cents"], "currency": "eur", "status": "succeeded",
                                           "livemode": False, "metadata": {"invoice_id": inv["id"], "company_id": inv["company_id"],
                                                                           "invoice_number": inv.get("number") or ""}}}}).encode()


def test_stripe_event_is_stored_queued_processed_and_a_failure_stays_retryable(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", WHSEC)
    monkeypatch.setattr(config, "STRIPE_API_KEY", "")

    async def go():
        from core.db import db
        from services import billing, payments, reconciliation
        inv = await _invoice(23456)
        body = _pi_event(inv)
        eid = json.loads(body)["id"]
        r = await reconciliation.receive_stripe_webhook(body, _sign(body))
        assert r["queued"] is True and r["event_id"] == eid
        ev = await db.raw.stripe_events.find_one({"id": eid}, {"_id": 0})
        assert ev["status"] == "received" and ev["verified"] is True and json.loads(ev["payload"]) == json.loads(body)
        assert ev["type"] == "payment_intent.succeeded" and ev["received_at"]
        jd = await db.raw.jobs.find_one({"key": f"stripe:{eid}"}, {"_id": 0})
        assert jd["kind"] == "payments.stripe_event" and jd["agent"] is False
        assert (await _inv(inv["id"]))["status"] == "open"  # nothing happened in the request itself
        await _work("payments.stripe_event")
        ev = await db.raw.stripe_events.find_one({"id": eid}, {"_id": 0})
        assert ev["status"] == "processed" and ev["processed_at"] and ev["error"] is None
        cur = await _inv(inv["id"])
        assert cur["status"] == "paid" and cur["payment"]["method"] == "card"
        assert await reconciliation.receive_stripe_webhook(body, _sign(body)) == {"ok": True, "duplicate": True}
        assert await payments.handle_webhook(body, _sign(body)) == {"ok": True, "duplicate": True}

        # Processing fails (the invoice is still a draft): the event stays failed and Stripe's retry is processed.
        draft = await _invoice(1000, issue=False)
        bad = _pi_event({**draft, "total_cents": 1255})
        bad_id = json.loads(bad)["id"]
        assert (await reconciliation.receive_stripe_webhook(bad, _sign(bad)))["queued"] is True
        await _work("payments.stripe_event")
        ev = await db.raw.stripe_events.find_one({"id": bad_id}, {"_id": 0})
        assert ev["status"] == "failed" and "draft" in ev["error"]
        jd = await db.raw.jobs.find_one({"key": f"stripe:{bad_id}"}, {"_id": 0})
        assert jd["status"] == "dead"
        assert await db.raw.dead_letters.find_one({"job_id": jd["id"], "status": "open"})
        await billing.issue_invoice(draft["id"], OPS, send=False)
        again = await reconciliation.receive_stripe_webhook(bad, _sign(bad))
        assert again.get("duplicate") is None and again["queued"] is True and again["retry"] is True
        assert (await db.raw.jobs.find_one({"key": f"stripe:{bad_id}"}))["status"] == "queued"
        await _work("payments.stripe_event")
        assert (await db.raw.stripe_events.find_one({"id": bad_id}))["status"] == "processed"
        cur = await _inv(draft["id"])
        assert cur["status"] == "paid" and cur["paid_cents"] == 1255
        assert (await db.raw.dead_letters.find_one({"job_id": jd["id"]}))["status"] == "retried"

        # Nothing to do: answered at once with the same answers as before, stored as processed.
        other = json.dumps({"id": "evt_s2_cus", "type": "customer.created", "data": {"object": {"id": "cus_1"}}}).encode()
        assert await reconciliation.receive_stripe_webhook(other, _sign(other)) == {"ok": True, "ignored": "customer.created"}
        assert (await db.raw.stripe_events.find_one({"id": "evt_s2_cus"}))["status"] == "processed"
        forged = json.dumps({"id": "evt_s2_cs", "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_test_nottracked", "object": "checkout.session", "payment_status": "paid", "status": "complete",
            "metadata": {}, "amount_total": 100, "currency": "eur"}}}).encode()
        assert (await reconciliation.receive_stripe_webhook(forged, _sign(forged)))["ok"] is False
        with pytest.raises(payments.WebhookRejected):
            await reconciliation.receive_stripe_webhook(body, _sign(body, "whsec_wrong"))
        with pytest.raises(payments.WebhookRejected):
            await reconciliation.receive_stripe_webhook(body, _sign(body, ts=int(time.time()) - 3600))
    run(go())


# --------------------------------------------------------------------------------------------- dunning
def _interest(principal: int, pct: float, days: int) -> int:
    v = Decimal(principal) * Decimal(str(pct)) / 100 * days / 365
    return int(v.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def test_dunning_stages_compensation_and_collection():
    async def go():
        from core.db import db
        from core.jobs import enqueue
        from routes.admin import DecisionIn, decide as decide_route
        from services import dunning
        invs = {d: await _invoice(50000 + d, due_days_ago=d) for d in (8, 22, 36, 46)}
        busy = await _invoice(7000, due_days_ago=9)
        await db.raw.payment_transactions.insert_one({"id": str(uuid.uuid4()), "provider": "stripe", "invoice_id": busy["id"],
                                                     "session_id": f"cs_test_{uuid.uuid4().hex}", "status": "processing",
                                                     "amount_cents": busy["total_cents"],
                                                     "created_at": iso(now() - timedelta(days=40))})
        assert all(i["status"] == "overdue" for i in invs.values())
        mails0 = await db.raw.email_outbox.count_documents({"tag": {"$regex": "^dunning_"}})
        await enqueue("dunning.run", {}, key="test:dunning:1")
        assert await _work("dunning.run") == 1
        job = await db.raw.jobs.find_one({"key": "test:dunning:1"}, {"_id": 0})
        assert job["status"] == "done" and job["agent"] is True

        expect = {8: "reminder_1", 22: "reminder_2", 36: "final_notice"}
        for days, stage in expect.items():
            cur = await _inv(invs[days]["id"])
            hist = cur["dunning"]["history"]
            assert cur["dunning"]["stage"] == stage and [h["stage"] for h in hist] == [stage]
            assert hist[0]["interest_cents"] == _interest(cur["total_cents"], 10.5, days)
            mail = await db.raw.email_outbox.find_one({"id": hist[0]["email_id"]}, {"_id": 0})
            assert mail["to"] == "laskut@testikuljetus.example" and mail["tag"] == f"dunning_{stage}"
            assert cur["number"] in mail["subject"] and IBAN in mail["text"] and cur["reference"] in mail["text"]
            assert dunning._eur(hist[0]["interest_cents"], "fi") in mail["text"] and "Korkoa ei laskuteta" in mail["text"]
            bell = await db.raw.notifications.find_one({"company_id": S["company"]["id"], "link": f"/portal/invoices/{cur['id']}"},
                                                       {"_id": 0})
            assert bell and bell["title_i18n"]["fi"].startswith(dunning.STAGE_LABEL[stage][0])
        assert (await db.raw.email_outbox.find_one({"id": (await _inv(invs[8]["id"]))["dunning"]["history"][0]["email_id"]}))[
            "subject"].startswith("Maksumuistutus: lasku")
        final = await db.raw.email_outbox.find_one({"id": (await _inv(invs[36]["id"]))["dunning"]["history"][0]["email_id"]})
        assert final["subject"].startswith("Viimeinen maksukehotus") and "siirretään perintään" in final["text"]

        comps = [c async for c in db.raw.invoices.find({"origin": "dunning"}, {"_id": 0})]
        assert sorted(c["dunning_for"] for c in comps) == sorted([invs[22]["id"], invs[36]["id"]])
        for c in comps:
            parent = await _inv(c["dunning_for"])
            assert parent["dunning"]["compensation_invoice_id"] == c["id"] == parent["dunning"]["history"][0]["compensation_invoice_id"]
            assert c["status"] == "open" and c["number"] and c["reference"] and c["total_cents"] == 4000 and c["vat_cents"] == 0
            assert [(ln["description"], ln["vat_code"]) for ln in c["lines"]] == [
                ("Vakiokorvaus perintäkuluista (korkolaki 10 a §)", "OUT_OF_SCOPE_DISBURSEMENT")]
            mail = await db.raw.email_outbox.find_one({"id": parent["dunning"]["history"][0]["email_id"]})
            assert c["number"] in mail["text"] and "10 a" in mail["text"]

        coll = await _inv(invs[46]["id"])
        assert not (coll.get("dunning") or {}).get("stage")
        card = await db.raw.approvals.find_one({"kind": "dunning_collection", "ref_id": coll["id"]}, {"_id": 0})
        assert card["status"] == "pending" and card["data"]["stage"] == "collection"
        assert [r["code"] for r in card["reasons"]] == ["collection_needs_owner"]
        assert await db.raw.email_outbox.count_documents({"ref.id": coll["id"], "tag": {"$regex": "^dunning_"}}) == 0
        assert not (await _inv(busy["id"])).get("dunning")  # card payment under way: no reminder

        n_mails = await db.raw.email_outbox.count_documents({"tag": {"$regex": "^dunning_"}})
        assert n_mails - mails0 == 3
        await enqueue("dunning.run", {}, key="test:dunning:2")
        await _work("dunning.run")
        assert await db.raw.email_outbox.count_documents({"tag": {"$regex": "^dunning_"}}) == n_mails
        assert await db.raw.invoices.count_documents({"origin": "dunning"}) == 2
        assert await db.raw.approvals.count_documents({"kind": "dunning_collection", "ref_id": coll["id"]}) == 1

        # Two servers executing the same stage at once: one wins, one e-mail.
        twin = await _invoice(1200, due_days_ago=10)
        results = await asyncio.gather(*[dunning.execute_stage(twin["id"], "reminder_1", expected_prev=None, actor=OPS)
                                         for _ in range(2)])
        assert sorted(bool(r.get("executed")) for r in results) == [False, True]
        assert len((await _inv(twin["id"]))["dunning"]["history"]) == 1
        assert await db.raw.email_outbox.count_documents({"ref.id": twin["id"], "tag": "dunning_reminder_1"}) == 1
        # The compare-and-set itself, with no read in between: from one state exactly one claim wins.
        solo = await _invoice(1300, due_days_ago=12)
        assert [await dunning.claim_stage(solo["id"], "reminder_1", None) for _ in range(2)] == [True, False]
        assert [await dunning.claim_stage(solo["id"], "reminder_2", "reminder_1") for _ in range(2)] == [True, False]
        declined = await _invoice(1400, due_days_ago=12)
        await db.raw.invoices.update_one({"id": declined["id"]}, {"$set": {"dunning": {"rejected": {"stage": "reminder_2"}}}})
        assert [await dunning.claim_stage(declined["id"], "reminder_1", None) for _ in range(2)] == [True, False]
        assert (await _inv(declined["id"]))["dunning"] == {**(await _inv(declined["id"]))["dunning"],
                                                         "stage": "reminder_1", "rejected": {"stage": "reminder_2"}}

        await decide_route(card["id"], DecisionIn(decision="approved", note="Perintään"), OPS)
        coll = await _inv(invs[46]["id"])
        assert coll["dunning"]["stage"] == "collection" and coll["collection"]["by"]["id"] == OPS["id"]
        owner_mail = await db.raw.email_outbox.find_one({"to": "owner@huolitsija.example", "subject": f"Perintään: lasku {coll['number']}"},
                                                        {"_id": 0})
        assert owner_mail and "perintätoimistoliittymää" in owner_mail["text"]
    run(go())


def test_dunning_with_autopilot_off_asks_the_owner_and_respects_a_decline():
    async def go():
        from core.db import db
        from core.jobs import enqueue
        from core.settings import update_settings
        from routes.admin import DecisionIn, decide as decide_route
        await update_settings({"autopilot_enabled": False})
        try:
            yes, no = await _invoice(2000, due_days_ago=9), await _invoice(2100, due_days_ago=10)
            await enqueue("dunning.run", {}, key="test:dunning:off:1")
            await _work("dunning.run")
            cards = {}
            for inv in (yes, no):
                cur = await _inv(inv["id"])
                assert not (cur.get("dunning") or {}).get("stage")
                card = await db.raw.approvals.find_one({"kind": "dunning_collection", "ref_id": inv["id"]}, {"_id": 0})
                assert card["status"] == "pending" and card["data"]["stage"] == "reminder_1"
                assert [r["code"] for r in card["reasons"]] == ["autopilot_off"] and card["title_fi"].startswith("Maksumuistutus")
                cards[inv["id"]] = card
            assert await db.raw.email_outbox.count_documents({"ref.id": {"$in": [yes["id"], no["id"]]}}) == 0
            await decide_route(cards[yes["id"]]["id"], DecisionIn(decision="approved"), OPS)
            await decide_route(cards[no["id"]]["id"], DecisionIn(decision="rejected", note="Sovittu maksuaika"), OPS)
            cur = await _inv(yes["id"])
            assert cur["dunning"]["stage"] == "reminder_1" and cur["dunning"]["history"][0]["by"]["id"] == OPS["id"]
            assert await db.raw.email_outbox.count_documents({"ref.id": yes["id"], "tag": "dunning_reminder_1"}) == 1
            assert (await _inv(no["id"]))["dunning"]["rejected"]["stage"] == "reminder_1"
            await enqueue("dunning.run", {}, key="test:dunning:off:2")
            await _work("dunning.run")
            assert await db.raw.approvals.count_documents({"kind": "dunning_collection", "ref_id": no["id"]}) == 1
            assert await db.raw.email_outbox.count_documents({"ref.id": no["id"]}) == 0
            assert await db.raw.approvals.count_documents({"kind": "dunning_collection", "ref_id": yes["id"]}) == 1
        finally:
            await update_settings({"autopilot_enabled": True})
    run(go())


# --------------------------------------------------------------------------------------------- routes (front door)
def _app():
    from fastapi import FastAPI, Request

    from core.deps import current_user
    from routes import invoices, payments_ops
    app = FastAPI()
    app.include_router(payments_ops.router)
    app.include_router(invoices.router)

    async def fake_user(request: Request):
        return VIEWER if request.headers.get("x-test-user") == "viewer" else OPS
    app.dependency_overrides[current_user] = fake_user
    return app


def test_routes_bank_webhook_upload_lists_and_reports(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", WHSEC)
    monkeypatch.setattr(config, "STRIPE_API_KEY", "")
    monkeypatch.delenv("BANK_WEBHOOK_SECRET", raising=False)

    async def go():
        import httpx

        from core.db import db
        inv = await _invoice(11100)
        a = _archive()
        data = camt054([{"cd": "CRDT", "amount": inv["total_cents"], "booking": _yesterday(), "archive": a,
                         "txs": [{"amount": inv["total_cents"], "archive": a, "debtor": "Testi Kuljetus Oy",
                                  "ref": inv["reference"]}]}])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
            assert (await c.post("/api/webhooks/bank/camt054", content=data)).status_code == 503
            os.environ["BANK_WEBHOOK_SECRET"] = "bank-secret-" + uuid.uuid4().hex
            sig = hmac.new(os.environ["BANK_WEBHOOK_SECRET"].encode(), data, hashlib.sha256).hexdigest()
            assert (await c.post("/api/webhooks/bank/camt054", content=data)).status_code == 401
            assert (await c.post("/api/webhooks/bank/camt054", content=data,
                                 headers={"X-Huolitsija-Signature": "0" * 64})).status_code == 401
            r = await c.post("/api/webhooks/bank/camt054", content=data,
                             headers={"X-Huolitsija-Signature": sig, "X-Filename": "nordea.xml"})
            assert r.status_code == 200 and r.json()["statement"]["status"] == "queued", r.text
            junk = b"not a statement"
            jsig = hmac.new(os.environ["BANK_WEBHOOK_SECRET"].encode(), junk, hashlib.sha256).hexdigest()
            r = await c.post("/api/webhooks/bank/camt054", content=junk, headers={"X-Huolitsija-Signature": jsig})
            assert r.status_code == 400 and "not valid XML" in r.json()["detail"]
            await _work("payments.camt054_import")
            assert (await _inv(inv["id"]))["status"] == "paid"

            r = await c.get("/api/autopilot/bank-transactions", params={"status": "matched"})
            assert r.status_code == 200 and a in [t["archive_ref"] for t in r.json()["items"]]
            assert all(t["status"] == "matched" for t in r.json()["items"]) and r.json()["counts"]["matched"] >= 1
            assert (await c.get("/api/autopilot/bank-transactions", params={"status": "bogus"})).status_code == 400

            up = camt054([{"cd": "CRDT", "amount": 777, "booking": _yesterday(), "archive": (b := _archive()),
                           "txs": [{"amount": 777, "archive": b, "debtor": "X Oy", "ref": "1232"}]}])
            r = await c.post("/api/autopilot/bank-statements", files={"file": ("lokakuu.xml", up, "application/xml")},
                             headers={"x-test-user": "viewer"})
            assert r.status_code == 403
            r = await c.post("/api/autopilot/bank-statements", files={"file": ("lokakuu.xml", up, "application/xml")})
            assert r.status_code == 200 and r.json()["filename"] == "lokakuu.xml" and r.json()["source"] == "upload"
            r = await c.post("/api/autopilot/bank-statements", files={"file": ("x.csv", b"a;b;c", "text/csv")})
            assert r.status_code == 400
            names = [s["filename"] for s in (await c.get("/api/autopilot/bank-statements")).json()["items"]]
            assert names[:2] == ["lokakuu.xml", "nordea.xml"]

            r = await c.post("/api/autopilot/reconciliation/run")
            assert r.status_code == 200 and r.json()["kind"] == "payments.reconcile_daily"
            await _work("payments.reconcile_daily")
            items = (await c.get("/api/autopilot/reconciliation-reports")).json()["items"]
            assert items and items == sorted(items, key=lambda x: x["created_at"], reverse=True)
            assert all(i["kind"] == "reconciliation" for i in items)
            one = await c.get(f"/api/autopilot/reconciliation-reports/{items[0]['id']}")
            assert one.status_code == 200 and one.json()["status"] in ("ok", "attention")
            assert (await c.get("/api/autopilot/reconciliation-reports/nope")).status_code == 404

            # Stripe webhook route: verified, stored, queued; the job books it.
            inv2 = await _invoice(5000)
            body = _pi_event(inv2)
            r = await c.post("/api/webhooks/stripe", content=body, headers={"Stripe-Signature": _sign(body)})
            assert r.status_code == 200 and r.json()["queued"] is True
            assert (await c.post("/api/webhooks/stripe", content=body,
                                 headers={"Stripe-Signature": "t=1,v1=deadbeef"})).status_code == 400
            await _work("payments.stripe_event")
            assert (await _inv(inv2["id"]))["status"] == "paid"
            r = await c.post("/api/webhooks/stripe", content=body, headers={"Stripe-Signature": _sign(body)})
            assert r.json() == {"ok": True, "duplicate": True}
        assert await db.raw.bank_statements.count_documents({"source": "webhook", "filename": "nordea.xml"}) == 1
    try:
        run(go())
    finally:
        os.environ.pop("BANK_WEBHOOK_SECRET", None)


# --------------------------------------------------------------------------------------------- real Stripe (test mode)
@pytest.mark.skipif(not os.environ.get("HUOLITSIJA_STRIPE_API_KEY"), reason="HUOLITSIJA_STRIPE_API_KEY not set")
def test_reconciliation_against_real_stripe_test_mode(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_API_KEY", os.environ["HUOLITSIJA_STRIPE_API_KEY"].strip())

    async def go():
        from core.db import db
        from services import payments, reconciliation
        inv = await _invoice(4321)
        out = await payments.create_checkout(inv, user={"id": OPS["id"], "name": OPS["name"], "email": "maksaja@testikuljetus.example",
                                                       "locale": "fi"}, base_url="https://huolitsija.example")
        assert out["session_id"].startswith("cs_test_")
        res = await reconciliation.run_reconciliation()
        rep = await db.raw.owner_reports.find_one({"id": res["report_id"]}, {"_id": 0})
        st = rep["data"]["stripe"]
        assert st["checked"] is True and st["mode"] == "test" and not st["errors"], st.get("errors")
        mine = [s for s in st["synced"] if s["session_id"] == out["session_id"]]
        assert mine == [{"session_id": out["session_id"], "invoice_id": inv["id"], "transaction_status": "initiated",
                         "payment_status": "unpaid", "applied_now": False}]
        assert st["sessions_listed"] >= 1 and out["session_id"] not in [r["session_id"] for r in st["recovered"]]
        assert (await _inv(inv["id"]))["status"] == "open"
        tx = await db.raw.payment_transactions.find_one({"session_id": out["session_id"]}, {"_id": 0})
        assert tx["status"] == "initiated" and tx["last_source"] == "poll" and tx["session_status"] == "open"
    run(go())


def test_debit_goes_to_the_payables_matcher_when_finance_is_installed(monkeypatch):
    import sys
    import types

    calls = []

    async def match_bank_debit(end_to_end_id, amount_cents, currency, booked_at):
        calls.append((end_to_end_id, amount_cents, currency, booked_at))
        return {"id": "pay-1"} if end_to_end_id == "E2E-PAY-1" else None

    async def go():
        from core.db import db
        from services import reconciliation
        txs = [{"id": f"tx-debit-{i}", "credit_debit": "debit", "entry_status": "BOOK", "amount_cents": 150000, "currency": "EUR",
                "booking_date": "2026-10-23", "end_to_end_id": e2e, "status": "new", "archive_ref": f"ARCH-DEBIT-{i}"}
               for i, e2e in enumerate(("E2E-PAY-1", "E2E-OTHER"))]
        await db.raw.bank_transactions.insert_many([dict(t) for t in txs])
        assert await reconciliation.match_transaction(txs[0]) == {"status": "matched", "payable_id": "pay-1"}
        assert (await reconciliation.match_transaction(txs[1]))["ignore_reason"] == "debit"
        row = await db.raw.bank_transactions.find_one({"id": "tx-debit-0"}, {"_id": 0})
        assert (row["status"], row["match_kind"], row["payable_id"], row["invoice_id"]) == ("matched", "payable", "pay-1", None)
        assert calls == [("E2E-PAY-1", 150000, "EUR", "2026-10-23"), ("E2E-OTHER", 150000, "EUR", "2026-10-23")]
        monkeypatch.delitem(sys.modules, "services.payables")
        import services
        monkeypatch.delattr(services, "payables", raising=False)
        # without finance installed the debit stays ignored (receivables only)
        txs[1].update(id="tx-debit-2", archive_ref="ARCH-DEBIT-2")
        await db.raw.bank_transactions.insert_one(dict(txs[1]))
        assert (await reconciliation.match_transaction(txs[1]))["ignore_reason"] == "debit" and len(calls) == 2

    fake = types.ModuleType("services.payables")
    fake.match_bank_debit = match_bank_debit
    monkeypatch.setitem(sys.modules, "services.payables", fake)
    import services
    monkeypatch.setattr(services, "payables", fake, raising=False)
    run(go())
