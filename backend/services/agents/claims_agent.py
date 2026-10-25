"""Claims agent: reviews a submitted cargo claim and pays it when it is clear-cut and inside the limits.

Paid automatically (Autopilot on): notice in time, evidence attached for damage/loss, the liability cap known (SDR
rate on file), an actual delay for a delay claim, and the amount = min(claimed, cap) within the claim limit
(guard_claim_limit_cents) and the insurer's settlement authority (insurance_claim_limit_cents, a legal human
point above it). Everything else goes to the owner with the agent's proposal; the agent never rejects a claim."""
from core.agent_runtime import _eur, agent_bot, approve_as_agent, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import JobDeferred, enqueue, job
from core.util import NO_ID, now, parse_iso
from services.agents.base import agents_epoch, customer_message, entity_lock, mode_key, sweeper

AGENT = "claims_agent"
EVIDENCE_WAIT_HOURS = 72


def _delay_hours(s: dict) -> float | None:
    planned = next((m.get("planned") for m in reversed(s.get("milestones") or []) if m.get("code") == "delivered"), None)
    actual = parse_iso(s.get("delivered_at"))
    p = parse_iso(planned)
    if not (actual and p):
        return None
    return round((actual - p).total_seconds() / 3600, 1)


@job("claims.decide", agent=True, max_attempts=4, backoff=(300, 1800, 3600, 7200), timeout=180,
     label_fi="Korvausvaatimuksen käsittely", label_en="Claim review")
async def decide_claim(j):
    from services import claims as svc
    cid = j["payload"]["claim_id"]
    async with entity_lock(f"claim:{cid}"):
        cl = await db.raw.claims.find_one({"id": cid}, NO_ID)
        if not cl or cl.get("demo") or cl["status"] not in ("submitted", "in_review") or cl.get("approval_id") \
                or cl.get("last_proposal_declined"):
            return {"skipped": (cl or {}).get("status")}  # a person declined a proposal: it stays with them
        s = await db.raw.shipments.find_one({"id": cl["shipment_id"]}, NO_ID) or {}
        bot = agent_bot(AGENT)
        reasons = []
        if cl.get("notice_late"):
            reasons.append(policy_reason("notice_late", "Reklamaatio tehtiin määräajan jälkeen", "Notice was given after the deadline"))
        if cl["type"] in ("damage", "loss") and not cl.get("document_ids"):
            created = parse_iso(cl.get("submitted_at") or cl.get("created_at")) or now()
            if (now() - created).total_seconds() < EVIDENCE_WAIT_HOURS * 3600:
                await customer_message(
                    cl["company_id"], cl["shipment_id"],
                    f"Käsittelemme korvausvaatimusta {cl['number']}. Liitä siihen valokuvat ja luovutuskuittaus tai "
                    f"rahtikirja, johon vahinko on merkitty, niin voimme ratkaista sen.",
                    f"We are handling claim {cl['number']}. Please attach photos and the delivery receipt or waybill "
                    f"with the damage noted, so that we can decide it.", subject=f"Reklamaatio {cl['number']}",
                    key=f"claim-evidence:{cid}")
                raise JobDeferred(3600, "waiting for the customer's evidence")
            reasons.append(policy_reason("no_evidence", "Vahingosta ei ole liitetty todisteita", "No evidence of the damage is attached"))
        cap = cl.get("cap_eur_cents")
        if cap is None:
            reasons.append(policy_reason("cap_unknown", "Vastuunrajoitusta ei voitu laskea (SDR-kurssi puuttuu)",
                                         "The liability cap could not be computed (no SDR rate)"))
        delay = _delay_hours(s) if cl["type"] == "delay" else None
        if cl["type"] == "delay" and not (delay and delay > 0):
            reasons.append(policy_reason("no_delay", "Toimitus ei näytä myöhästyneen suunnitellusta", "The delivery does not show a delay"))
        claimed = int(cl["amount_claimed_cents"])
        amount = min(claimed, int(cap)) if cap is not None else claimed
        basis = cl.get("cap_basis") or cl.get("regime_label")
        ndocs = len(cl.get("document_ids") or [])
        type_fi = {"damage": "vaurio", "loss": "katoaminen", "delay": "viivästys"}.get(cl["type"], cl["type"])
        note = (f"Autopilot: {cl['type']} claim, notice in time, {ndocs} document(s); "
                f"paid {amount / 100:.2f} EUR = min(claimed {claimed / 100:.2f}, cap {((cap or 0) / 100):.2f}) under {basis}.")
        note_fi = (f"Autopilotti: {type_fi}, reklamaatio ajoissa, {ndocs} liitettä; korvaus {_eur(amount)} = pienempi "
                   f"vaaditusta {_eur(claimed)} ja vastuurajasta {_eur(cap)} ({basis}).")
        d = await decide(AGENT, "decide_claim", subject=("claim", cid), subject_label=cl["number"], company_id=cl["company_id"],
                         inputs={"type": cl["type"], "claimed_cents": claimed, "cap_eur_cents": cap, "regime": cl.get("liability_regime"),
                                 "notice_late": cl.get("notice_late"), "documents": len(cl.get("document_ids") or []),
                                 "delay_hours": delay},
                         decision={"decision": "approve", "amount_cents": amount}, confidence=None, claim_cents=amount,
                         extra_reasons=reasons, create_approval=False, title=f"Claim {cl['number']}",
                         title_fi=f"Reklamaatio {cl['number']}")
        if cl["status"] == "submitted":
            await svc.start_review(cid, bot)
        extra = {"agent_decision_id": d.id, "agent": AGENT, "reasons": d.reasons,
                 "reason_fi": "; ".join(r["fi"] for r in d.reasons), "title_fi": f"Reklamaatio {cl['number']}: korvaus"}
        if d.row and d.row.get("legal_point"):
            from core.legal_points import LEGAL_POINTS
            lp = LEGAL_POINTS[d.row["legal_point"]]
            extra.update(legal_point=d.row["legal_point"], legal_basis=lp["basis"], legal_reason_fi=lp["reason_fi"],
                         legal_reason_en=lp["reason_en"], legal_title_fi=lp["title_fi"], legal_title_en=lp["title_en"])
        if d.auto:
            extra["agent_decides"] = True
        cl = await svc.propose_decision(cid, decision="approve", amount_cents=amount, note=note, actor=bot, approval_extra=extra)
        await db.raw.claims.update_one({"id": cid}, {"$set": {"proposal.note_i18n": {"fi": note_fi, "en": note}}})
        await db.raw.agent_decisions.update_one({"id": d.id}, {"$set": {"approval_id": cl.get("approval_id"),
                                                                        "outcome": "auto" if d.auto else "owner_queue"}})
        if not d.auto:
            return {"owner_queue": cl.get("approval_id"), "reasons": [r["code"] for r in d.reasons]}
        a = await db.raw.approvals.find_one({"id": cl["approval_id"]}, NO_ID)
        await approve_as_agent(a, AGENT, note)
        return {"approved_cents": amount}


@on("claim.submitted")
async def _on_submitted(p):
    await enqueue("claims.decide", {"claim_id": p["claim_id"]}, company_id=p.get("company_id"), subject=("claim", p["claim_id"]),
                  key=f"claim:{p['claim_id']}:{await mode_key()}")


@sweeper
async def sweep_claims():
    n = 0
    mk = await mode_key()
    async for c in db.raw.claims.find({"status": "submitted", "demo": {"$ne": True}, "approval_id": None,
                                       "created_at": {"$gte": await agents_epoch()}}, {"_id": 0, "id": 1, "company_id": 1}).limit(50):
        await enqueue("claims.decide", {"claim_id": c["id"]}, company_id=c.get("company_id"), subject=("claim", c["id"]),
                      key=f"claim:{c['id']}:{mk}", source="sweep")
        n += 1
    return n
