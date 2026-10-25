"""One inbound pipeline for every mail source (Postmark/CloudMailin webhook, mail.tm poller): RFQ triage first, then
CRM replies, accounts payable, and customer intake last. Raises ValueError for an unreadable AP envelope."""
from __future__ import annotations

from . import inbound


async def dispatch_raw(raw: bytes, *, provider_id: str | None = None, payload: dict | None = None,
                       provider_authenticated: bool = False, source: str = "webhook") -> dict:
    from services.carrier_booking_mail import try_ingest as carrier_reply
    reply = await carrier_reply(raw, authenticated=provider_authenticated, provider_id=provider_id)
    if reply is not None:
        return {"ok": True, "channel": "carrier_booking", **reply}
    # RFQ mail must never enter customer signup, including unknown references and replies from a different sender.
    from services.partner_network import try_ingest_rfq
    rfq = await try_ingest_rfq(raw, provider_id=provider_id, authenticated_provider=provider_authenticated)
    if rfq is not None:
        return {"ok": True, "channel": "partner_network", **rfq}
    # A reply to a prospect e-mail updates its CRM lead (CRM_REPLY_TO is the inbound address; IMAP is optional).
    from services.prospecting import try_ingest_reply
    crm_reply = await try_ingest_reply(raw)
    if crm_reply is not None:
        return {"ok": True, "channel": "crm", **crm_reply}
    # Explicit AP envelope routing follows RFQ triage and precedes customer intake.
    from services.ap_postmark import try_ingest_postmark
    ap = await try_ingest_postmark(raw, payload=payload, authenticated_provider=provider_authenticated)
    if ap is not None:
        return {"ok": True, "channel": "accounts_payable", **ap}
    row, created = await inbound.ingest(raw, source=source)
    return {"ok": True, "id": row["id"], "duplicate": not created}
