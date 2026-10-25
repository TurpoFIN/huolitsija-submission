"""Invoice agent: issues the invoice when a shipment is delivered. The draft comes from services.billing (quote
lines, extras, customs pass-through); customs lines wait for Tulli's decision (the job waits, then continues on
customs.decision_recorded). A clean draft is issued and e-mailed with its viitenumero / RF reference; a draft with
warnings, or Autopilot off, goes to the owner queue (invoice_issue)."""
from core import config
from core.agent_runtime import agent_bot, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import JobDeferred, enqueue, job
from core.util import NO_ID, now, parse_iso
from services.agents.base import agents_epoch, entity_lock, mode_key, sweeper

AGENT = "invoice_agent"
CUSTOMS_WAIT_HOURS = float(config.env("INVOICE_CUSTOMS_WAIT_HOURS", "72"))
PENDING_CUSTOMS = ("The import declaration is not released yet", "Record Tulli's decided duty and import VAT")


def _customs_pending(inv: dict) -> bool:
    return any(str(w).startswith(PENDING_CUSTOMS) for w in inv.get("warnings") or [])


@job("invoice.issue_on_delivery", agent=True, max_attempts=6, backoff=(60, 300, 900, 1800, 3600, 7200), timeout=300,
     label_fi="Laskutus toimituksen jälkeen", label_en="Invoice on delivery")
async def issue_on_delivery(j):
    from services import billing
    sid = j["payload"]["shipment_id"]
    async with entity_lock(f"invoice:{sid}"):
        s = await db.raw.shipments.find_one({"id": sid}, NO_ID)
        if not s or s.get("demo") or s.get("status") != "delivered":
            return {"skipped": "not delivered"}
        try:
            inv = await billing.create_invoice_for_shipment(sid, None)
        except billing.BillingError as exc:
            d = await decide(AGENT, "draft_invoice", subject=("shipment", sid), subject_label=s.get("reference"),
                             company_id=s.get("company_id"), extra_reasons=[policy_reason("billing_error", str(exc), str(exc))],
                             approval_kind="invoice_issue", title=f"Invoice {s.get('reference')}",
                             title_fi=f"Laskuta {s.get('reference')}", link=f"/ops/shipments/{sid}",
                             approval_data={"shipment_id": sid})
            return {"owner_queue": d.approval and d.approval["id"], "error": str(exc)}
        if inv["status"] != "draft":
            return {"skipped": inv["status"], "number": inv.get("number")}
        if _customs_pending(inv):
            delivered = parse_iso(s.get("delivered_at")) or now()
            if (now() - delivered).total_seconds() < CUSTOMS_WAIT_HOURS * 3600:
                raise JobDeferred(300, "waiting for Tulli's decision before the customs lines can be invoiced")
        return await gate_and_issue(inv, s)


async def gate_and_issue(inv: dict, s: dict | None) -> dict:
    from services import billing
    warnings = [w for w in inv.get("warnings") or []]
    extra = [policy_reason("invoice_warning", (billing.text_i18n(w) or {}).get("fi") or w, w) for w in warnings[:4]]
    if inv.get("total_cents", 0) <= 0:
        extra.append(policy_reason("zero_total", "Laskun summa on nolla", "The invoice total is zero"))
    ref = (s or {}).get("reference") or inv.get("shipment_reference")
    lines = [{"description": ln.get("description"), "amount_cents": ln.get("amount_cents"), "vat_code": ln.get("vat_code"),
              "code": ln.get("code")} for ln in inv.get("lines") or []]
    d = await decide(AGENT, "issue_invoice", subject=("invoice", inv["id"]), subject_label=ref,
                     company_id=inv.get("company_id"), inputs={"lines": lines, "warnings": warnings, "info": inv.get("info")},
                     decision={"issue": True, "total_cents": inv.get("total_cents"), "vat_cents": inv.get("vat_cents")},
                     confidence=None, extra_reasons=extra, approval_kind="invoice_issue",
                     title=f"Issue invoice for {ref}", title_fi=f"Lähetä lasku: {ref}",
                     summary=f"{inv.get('company_name')}: {inv.get('total_cents', 0) / 100:,.2f} EUR",
                     link=f"/ops/billing/{inv['id']}", amount_cents=inv.get("total_cents"),
                     approval_data={"invoice_id": inv["id"], "shipment_id": inv.get("shipment_id")})
    if not d.auto:
        return {"owner_queue": d.approval and d.approval["id"], "invoice_id": inv["id"]}
    out = await billing.issue_invoice(inv["id"], agent_bot(AGENT), send=True)
    return {"issued": out.get("number"), "reference": out.get("reference"), "total_cents": out.get("total_cents")}


@job("invoice.issue", agent=True, max_attempts=4, timeout=180, label_fi="Laskun lähetys", label_en="Issue invoice")
async def issue_draft(j):
    inv = await db.raw.invoices.find_one({"id": j["payload"]["invoice_id"]}, NO_ID)
    if not inv:
        return {"skipped": "no invoice"}
    async with entity_lock(f"invoice:{inv.get('shipment_id') or inv['id']}"):
        inv = await db.raw.invoices.find_one({"id": inv["id"]}, NO_ID)
        if inv["status"] != "draft" or inv.get("demo"):
            return {"skipped": "not a draft"}
        s = await db.raw.shipments.find_one({"id": inv.get("shipment_id")}, NO_ID) if inv.get("shipment_id") else None
        return await gate_and_issue(inv, s)


@on("shipment.delivered")
async def _on_delivered(p):
    s = p.get("shipment") or {}
    if s.get("id") and not s.get("demo"):
        await enqueue("invoice.issue_on_delivery", {"shipment_id": s["id"]}, company_id=s.get("company_id"),
                      subject=("shipment", s["id"]), key=f"invoice:{s['id']}:delivered")


@on("customs.decision_recorded")
async def _on_decision(p):
    d = p.get("declaration") or {}
    if d.get("shipment_id"):
        await enqueue("invoice.issue_on_delivery", {"shipment_id": d["shipment_id"]}, company_id=d.get("company_id"),
                      subject=("shipment", d["shipment_id"]), key=f"invoice:{d['shipment_id']}:decision:{d['id']}")


@on("approval.decided")
async def _on_approved(p):
    a = p["approval"]
    if a.get("kind") != "invoice_issue" or a["status"] != "approved":
        return
    from services import billing
    data = a.get("data") or {}
    inv_id = data.get("invoice_id")
    if not inv_id and data.get("shipment_id"):
        inv = await billing.create_invoice_for_shipment(data["shipment_id"], None)
        inv_id = inv["id"]
    inv = await db.raw.invoices.find_one({"id": inv_id}, NO_ID) if inv_id else None
    if inv and inv["status"] == "draft":
        await billing.issue_invoice(inv["id"], p.get("actor") or agent_bot(AGENT), send=True)


@sweeper
async def sweep_invoices():
    n = 0
    mk = await mode_key()
    async for s in db.raw.shipments.find({"status": "delivered", "demo": {"$ne": True}, "created_at": {"$gte": await agents_epoch()}},
                                         {"_id": 0, "id": 1, "company_id": 1}).limit(100):
        inv = await db.raw.invoices.find_one({"shipment_id": s["id"], "kind": "invoice", "origin": "shipment",
                                              "status": {"$ne": "void"}}, {"_id": 0, "status": 1})
        if inv and inv["status"] != "draft":
            continue
        if await db.raw.approvals.find_one({"kind": "invoice_issue", "status": "pending", "data.shipment_id": s["id"]}, {"_id": 1}):
            continue
        await enqueue("invoice.issue_on_delivery", {"shipment_id": s["id"]}, company_id=s.get("company_id"),
                      subject=("shipment", s["id"]), key=f"invoice:{s['id']}:sweep:{mk}", source="sweep")
        n += 1
    return n
