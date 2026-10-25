"""Send a trade document to the parties that need it, in their language, and record every send.

    result = await send(doc_id, milestone="departed")          # gate -> recipients -> e-mail (+ bell for the customer)

Recipients per kind are in catalog.KINDS[kind]["to"]: customer (the booking company's users), shipper, consignee, notify
(their e-mail on the shipment), carrier (the main leg's carrier contact), delivery_carrier (last leg), chamber
(COO_CHAMBER_EMAIL). Finnish parties get Finnish, everyone else English.

Every e-mail carries a signed download link per language and format (valid 30 days, HMAC with JWT_SECRET); when
core.email.send_email accepts `attachments`, the PDF (and the carrier's structured file) is attached as well.

Autopilot: sending to anyone but the own customer is an agent action. It goes through
core.agent_runtime.decide("trade_documents", "send_<kind>") (s2) - kill switch, Autopilot switch, legal points - and
waits in the owner queue (approval kind document_review) when not automatic. Without s2's runtime the same rule is
applied from the settings (autopilot_enabled and not autopilot_halted) and the card is a plain approval.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import logging
import time

from core import config
from core.audit import actor_ref, audit
from core.db import db
from core.email import render, send_email
from core.events import emit
from core.notify import bi, notify_company
from core.util import NO_ID, now_iso
from services.tradedocs import catalog

log = logging.getLogger("tradedocs.dispatch")
AGENT = "trade_documents"
LINK_DAYS = 30


# ------------------------------------------------------------------------------------------------ signed links
def _secret() -> bytes:
    return (config.JWT_SECRET or "huolitsija-tradedocs").encode()


def sign(doc_id: str, part: str, exp: int) -> str:
    msg = f"{doc_id}:{part}:{exp}".encode()
    return base64.urlsafe_b64encode(hmac.new(_secret(), msg, hashlib.sha256).digest()[:18]).decode().rstrip("=")


def link(doc_id: str, part: str, days: int = LINK_DAYS) -> str:
    """part: pdf_fi | pdf_en | <structured format>."""
    exp = int(time.time()) + days * 86400
    base = (config.APP_BASE_URL or "").rstrip("/")
    return f"{base}/api/trade-documents/{doc_id}/download/{part}?exp={exp}&sig={sign(doc_id, part, exp)}"


def check(doc_id: str, part: str, exp: int, sig: str) -> bool:
    return exp >= int(time.time()) and hmac.compare_digest(sign(doc_id, part, exp), sig or "")


# ------------------------------------------------------------------------------------------------ the Autopilot gate
async def gate(action: str, doc: dict, *, legal_point: str | None = None, title: str, title_fi: str, summary: str = "",
               summary_fi: str = "", inputs: dict | None = None) -> dict:
    """{auto, outcome, decision_id, approval_id}. Every call leaves an audit row."""
    link_ui = f"/ops/shipments/{doc['shipment_id']}"
    data = {"trade_document_id": doc["id"], "doc_no": doc["doc_no"], "action": action}
    try:
        from core.agent_runtime import decide
    except ImportError:
        decide = None
    if decide:
        d = await decide(AGENT, action, subject=("trade_document", doc["id"]), subject_label=f"{doc['doc_no']} v{doc['version']}",
                         company_id=doc.get("company_id"), inputs=inputs or {"kind": doc["kind"], "version": doc["version"]},
                         decision={"action": action}, confidence=None, legal_point=legal_point, approval_kind="trade_document",
                         title=title, title_fi=title_fi, summary=summary, summary_fi=summary_fi or summary, link=link_ui,
                         approval_data=data)
        return {"auto": d.auto, "outcome": d.outcome, "decision_id": d.id, "approval_id": (d.approval or {}).get("id"),
                "reasons": d.reasons}
    from core.approvals import request_approval
    from core.settings import get_settings
    st = await get_settings()
    auto = bool(st.get("autopilot_enabled")) and not st.get("autopilot_halted") and not legal_point
    if auto:
        from core import autonomy
        await autonomy.action(f"tradedoc_{action}", "trade_document", doc["id"], company_id=doc.get("company_id"), data=data)
        return {"auto": True, "outcome": "auto", "decision_id": None, "approval_id": None, "reasons": []}
    card = await request_approval("trade_document", "trade_document", doc["id"], title, summary=summary,
                                  reason="Autopilot is off" if not st.get("autopilot_enabled") else (legal_point or "halted"),
                                  company_id=doc.get("company_id"), data=data, link=link_ui)
    await audit(None, "tradedoc.owner_queue", "trade_document", doc["id"], {"action": action, "approval_id": card["id"]},
                company_id=doc.get("company_id"))
    return {"auto": False, "outcome": "owner_queue", "decision_id": None, "approval_id": card["id"], "reasons": []}


# ------------------------------------------------------------------------------------------------ recipients
def _lang_for(country: str | None) -> str:
    return "fi" if (country or "").upper() == "FI" else "en"


async def recipients(doc: dict) -> list[dict]:
    ctx = doc["data"]
    out: list[dict] = []
    seen: set[str] = set()
    customer_name = ((ctx.get("customer") or {}).get("name") or "").strip().lower()

    def add(role: str, email: str | None, name: str | None, lang: str, *, internal_customer=False, user=None):
        if not email or email.lower() in seen:
            return
        seen.add(email.lower())
        out.append({"role": role, "email": email, "name": name, "lang": lang, "customer": internal_customer,
                    "user_id": (user or {}).get("id")})

    async def customer_users(role: str):
        """The customer's users who take e-mail; when none does, the company's documents / billing address (documents
        are part of the contract, not a notification the user can mute)."""
        n = 0
        async for u in db.users.find({"company_id": doc.get("company_id"), "disabled": {"$ne": True}},
                                     {"_id": 0, "id": 1, "email": 1, "name": 1, "locale": 1, "notify_email": 1}):
            if u.get("notify_email", True):
                add(role, u.get("email"), u.get("name"), "en" if u.get("locale") == "en" else "fi", internal_customer=True, user=u)
                n += 1
        if not n:
            c = await db.companies.find_one({"id": doc.get("company_id")}, {"_id": 0, "documents_email": 1, "billing_email": 1,
                                                                              "email": 1, "name": 1, "country": 1})
            c = c or {}
            add(role, c.get("documents_email") or c.get("billing_email") or c.get("email"), c.get("name"),
                _lang_for(c.get("country") or "FI"), internal_customer=True)

    for role in catalog.KINDS[doc["kind"]]["to"]:
        if role == "customer":
            await customer_users("customer")
        elif role in ("shipper", "consignee", "notify"):
            p = ctx.get(role)
            if not p:
                continue
            if (p.get("name") or "").strip().lower() == customer_name:
                await customer_users(role)  # the customer is this party: its own users receive it
            elif p.get("email"):
                add(role, p["email"], p.get("name"), _lang_for((p.get("address") or {}).get("country")))
            else:
                out.append({"role": role, "email": None, "name": p.get("name"), "lang": None, "customer": False,
                            "missing": "no e-mail address on the shipment"})
        elif role == "carrier":
            leg = ctx.get("main_leg") or {}
            if doc["kind"] == "cmr" and ctx.get("road_legs_international"):
                leg = ctx["road_legs_international"][0]
            email = leg.get("carrier_email") or next((lg.get("carrier_email") for lg in ctx.get("legs") or []
                                                      if lg.get("carrier") == leg.get("carrier") and lg.get("carrier_email")), None)
            if email:
                add("carrier", email, leg.get("carrier"), "en")
            elif leg.get("carrier"):
                out.append({"role": "carrier", "email": None, "name": leg.get("carrier"), "lang": None, "customer": False,
                            "missing": "carrier has no contact e-mail"})
        elif role == "delivery_carrier":
            dc = ctx.get("delivery_carrier") or {}
            if dc.get("email"):
                add("delivery_carrier", dc["email"], dc.get("name"), _lang_for(ctx["destination"].get("country")))
            elif dc.get("name"):
                out.append({"role": "delivery_carrier", "email": None, "name": dc["name"], "lang": None, "customer": False,
                            "missing": "carrier has no contact e-mail"})
        elif role == "chamber":
            email = config.env("COO_CHAMBER_EMAIL")
            if email:
                add("chamber", email, "Chamber of Commerce", "fi")
    return out


# ------------------------------------------------------------------------------------------------ the e-mail
TEXT = {
    "subject": ("{title} {no} · {ref}", "{title} {no} · {ref}"),
    "intro_new": ("Liitteenä {title_l} {no} (versio {v}) lähetykselle {ref}.", "Please find {title_l} {no} (version {v}) for shipment {ref}."),
    "intro_amend": ("{title} {no} on korjattu (versio {v}): {reason}. Edellinen versio on korvattu.",
                    "{title} {no} has been amended (version {v}): {reason}. The previous version is superseded."),
    "draft": ("Tämä on luonnos tarkistettavaksi. Ilmoita korjaukset vastaamalla tähän viestiin.",
              "This is a draft for your review. Reply to this message with any corrections."),
    "route": ("Reitti: {o} → {d}.", "Route: {o} → {d}."),
    "download": ("Lataa PDF", "Download PDF"),
}


def _t(key: str, lang: str, **kw) -> str:
    fi, en = TEXT[key]
    return (fi if lang == "fi" else en).format(**kw)


def compose(doc: dict, lang: str) -> tuple[str, str, str]:
    ctx = doc["data"]
    title = catalog.KINDS[doc["kind"]]["title"][lang]
    kw = {"title": title, "title_l": title[0].lower() + title[1:] if lang == "fi" else title.lower(), "no": doc["doc_no"],
          "v": doc["version"], "ref": ctx["shipment"]["reference"], "o": ctx["origin"].get("name"), "d": ctx["destination"].get("name"),
          "reason": ((doc.get("amendment") or {}).get("reason") or "")}
    subject = _t("subject", lang, **kw)
    lines = [_t("intro_amend" if doc.get("amendment") else "intro_new", lang, **kw), _t("route", lang, **kw)]
    if doc["status"] == "draft":
        lines.append(_t("draft", lang, **kw))
    other = "en" if lang == "fi" else "fi"
    lines.append(("Englanninkielinen versio" if lang == "fi" else "Finnish version") + f": {link(doc['id'], 'pdf_' + other)}")
    for fmt in (doc.get("structured") or {}):
        lines.append(f"{fmt}: {link(doc['id'], fmt)}")
    lines.append(("Tarkista aitous" if lang == "fi" else "Verify") + f": {(config.APP_BASE_URL or '').rstrip('/')}"
                 f"/api/trade-documents/verify/{doc['doc_no']}?v={doc['version']}")
    html, text = render(subject, lines, link(doc["id"], "pdf_" + lang), _t("download", lang, **kw))
    return subject, html, text


def _supports_attachments() -> bool:
    try:
        return "attachments" in inspect.signature(send_email).parameters
    except (TypeError, ValueError):
        return False


async def _attachments(doc: dict, lang: str, role: str) -> list[tuple[str, str, bytes]]:
    from services.tradedocs import service
    blob, name = await service.pdf_bytes(doc["id"], lang)
    files = [(name, "application/pdf", blob)]
    fmt = {"hawb": "xfzb", "cmr": "ecmr", "house_bl": "dcsa_ebl_3"}.get(doc["kind"])
    if role in ("carrier", "delivery_carrier") and fmt and fmt in (doc.get("structured") or {}):
        import json
        body = doc["structured"][fmt]
        data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1).encode()
        files.append((f"{doc['doc_no']}-v{doc['version']}.{fmt}.{'xml' if fmt == 'xfzb' else 'json'}",
                      "application/xml" if fmt == "xfzb" else "application/json", data))
    return files


async def send(doc_id: str, *, milestone: str | None = None, actor: dict | None = None, gated: bool = True) -> dict:
    # The atomic Mongo lease covers the read/check/send/receipt write and also
    # prevents amendment during dispatch. A second API request gets 409; durable
    # jobs retry. Re-read the record inside the lease for current dedup receipts.
    from services.tradedocs import service
    doc = await service.get(doc_id)
    if not doc:
        return {"error": "not found"}
    async with service._version_lock(doc["shipment_id"], doc["kind"]):
        return await _send(doc_id, milestone=milestone, actor=actor, gated=gated)


async def _send(doc_id: str, *, milestone: str | None = None, actor: dict | None = None, gated: bool = True) -> dict:
    """Send the document's current version. Idempotent per (version, recipient). Returns {sent, skipped, waiting, missing}."""
    from services.tradedocs import service
    doc = await service.get(doc_id)
    if not doc:
        return {"error": "not found"}
    if doc["status"] in ("cancelled", "superseded"):
        return {"skipped": f"document is {doc['status']}"}
    if doc["kind"] == "delivery_order" and doc["status"] == "issued":
        from services.tradedocs.release import validate
        await validate(doc["data"])
    if doc["kind"] == "arrival_notice" and gated and actor is None:
        from services.tradedocs.jobs import arrival_evidence
        if not arrival_evidence(await service._shipment(doc["shipment_id"])):
            return {"waiting": {"reason": "Recorded arrival evidence is required"}}
    recips = await recipients(doc)
    external = [r for r in recips if r.get("email") and not r["customer"]]
    result = {"sent": [], "skipped": [], "waiting": None, "missing": [r for r in recips if not r.get("email")], "failed": [], "logged": []}
    if external and gated and actor is None:
        g = await gate(f"send_{doc['kind']}", doc,
                       legal_point="dangerous_goods" if doc["kind"] == "dg_declaration" else None,
                       title=f"Send {doc['doc_no']} ({catalog.KINDS[doc['kind']]['title']['en']}) to {', '.join(r['role'] for r in external)}",
                       title_fi=f"Lähetä {doc['doc_no']} ({catalog.KINDS[doc['kind']]['title']['fi']}): {', '.join(r['role'] for r in external)}",
                       summary=f"{doc['shipment_reference']} · version {doc['version']}",
                       inputs={"kind": doc["kind"], "version": doc["version"], "recipients": [r["email"] for r in external],
                               "milestone": milestone})
        if not g["auto"]:
            result["waiting"] = g
            external = []  # the customer's own copy still goes out
            recips = [r for r in recips if r["customer"]]
    already = {(d.get("version"), (d.get("to") or "").lower()) for d in doc.get("dispatches") or [] if d.get("status") == "sent"}
    attach = _supports_attachments()
    for r in recips:
        if not r.get("email"):
            continue
        if (doc["version"], r["email"].lower()) in already:
            result["skipped"].append(r["email"])
            continue
        lang = r["lang"] or "en"
        subject, html, text = compose(doc, lang)
        kwargs = {"tag": f"tradedoc_{doc['kind']}", "company_id": doc.get("company_id"),
                  "ref": {"type": "trade_document", "id": doc["id"], "doc_no": doc["doc_no"], "version": doc["version"],
                          "shipment_id": doc["shipment_id"]}}
        if attach:
            kwargs["attachments"] = await _attachments(doc, lang, r["role"])
        rec = await send_email(r["email"], subject, html, text, **kwargs)
        entry = {"at": now_iso(), "version": doc["version"], "to": r["email"], "role": r["role"], "name": r.get("name"),
                 "lang": lang, "channel": "email", "status": rec.get("status"), "email_id": rec.get("id"),
                 "attachments": bool(attach), "milestone": milestone, "by": actor_ref(actor) if actor else
                 {"type": "agent", "id": AGENT, "name": "AI: trade documents"}}
        await db.trade_documents.update_one({"id": doc["id"]}, {"$push": {"dispatches": entry}})
        outcome = "sent" if rec.get("status") == "sent" else "logged" if rec.get("status") == "logged" else "failed"
        result[outcome].append(entry)
        if outcome == "sent":
            await emit("tradedoc.sent", {"document": {"id": doc["id"], "doc_no": doc["doc_no"], "kind": doc["kind"]}, "dispatch": entry})
    if result["sent"] and doc.get("company_id"):
        title = catalog.KINDS[doc["kind"]]["title"]
        await notify_company(doc["company_id"], bi(f"{title['fi']} {doc['doc_no']}", f"{title['en']} {doc['doc_no']}"),
                             bi(f"Versio {doc['version']} lähetetty: {', '.join(e['role'] for e in result['sent'])}",
                                f"Version {doc['version']} sent: {', '.join(e['role'] for e in result['sent'])}"),
                             link=f"/portal/shipments/{doc['shipment_id']}", kind="document")
    await audit(actor, "tradedoc.dispatched", "trade_document", doc["id"],
                {"doc_no": doc["doc_no"], "version": doc["version"], "milestone": milestone,
                 "sent": [e["to"] for e in result["sent"]], "missing": [m["role"] for m in result["missing"]],
                 "waiting": (result["waiting"] or {}).get("approval_id")}, company_id=doc.get("company_id"))
    return result
