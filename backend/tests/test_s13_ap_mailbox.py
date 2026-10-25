"""s13 purchase-invoice inbox: a carrier e-mails a Finvoice to ostolaskut@huolitsija.test (greenmail SMTP), the backend
reads it over IMAP (POST /api/finance/ap-mailbox/poll) and the invoice is ingested with source email.

Needs the backend started with AP_IMAP_HOST=127.0.0.1 AP_IMAP_PORT=3143 AP_IMAP_SECURITY=none
AP_IMAP_USER=ostolaskut@huolitsija.test AP_IMAP_PASSWORD=sandbox (sandbox values)."""
from __future__ import annotations

import time

from test_s13_ap_support import (AP_MAILBOX, accrue, ap_shipment, autopilot, db, finvoice_for, fi_iban, imap_unseen_ids,
                                 make_carrier, ops, send_mail)

FREIGHT = 2 * 187_654 + 2 * 11_728
DELIVERY = 30_150


def test_emailed_finvoice_is_ingested_matched_and_marked_seen():
    carrier = make_carrier("FI")
    s = ap_shipment(carrier)
    accrue(s)
    xml, inv = finvoice_for(s, carrier, fi_iban(), freight_net=FREIGHT, delivery_net=DELIVERY)
    msg_id = send_mail(AP_MAILBOX, f"Lasku {inv['number']}", [(f"finvoice_{inv['number']}.xml", xml, "application/xml")],
                       sender=carrier["contact_email"])
    with autopilot(False):
        r = ops().post("/finance/ap-mailbox/poll")
        assert r.status_code == 200, r.text
        deadline = time.time() + 60
        ci = None
        while time.time() < deadline:
            ci = db.carrier_invoices.find_one({"message_id": msg_id})
            if ci and ci["status"] not in ("received", "extracting") and ci.get("decision"):
                break
            time.sleep(1)
    assert ci, "the e-mailed invoice was not ingested"
    assert ci["source"] == "email" and ci["sender"] == carrier["contact_email"] and ci["number"] == inv["number"]
    assert ci["status"] == "matched" and ci["matches"][0]["shipment_ref"] == s["reference"]
    row = db.ap_messages.find_one({"message_id": msg_id})
    assert row and row["carrier_invoice_ids"] == [ci["id"]] and row["from"] == carrier["contact_email"]
    assert row["attachments"][0]["filename"] == f"finvoice_{inv['number']}.xml"
    assert msg_id not in imap_unseen_ids(AP_MAILBOX), "the message was not flagged \\Seen on the IMAP server"
    # a second poll does not ingest it again
    assert ops().post("/finance/ap-mailbox/poll").status_code == 200
    assert db.carrier_invoices.count_documents({"message_id": msg_id}) == 1
