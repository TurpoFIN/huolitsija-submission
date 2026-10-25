"""KYC agent: runs the register and sanctions checks as soon as a customer's profile is complete and decides within
the risk rules. Low risk (every risk check green) is approved when Autopilot is on; anything else, and everything
while Autopilot is off, becomes an owner-queue card. Sanctions matches, Russia/Belarus and politically exposed owners
are legal human points and never pass without a person."""
from core.agent_runtime import Decision, agent_bot, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import JobDeferred, enqueue, job
from core.util import NO_ID, now_iso
from services.agents.base import mode_key, sweeper

AGENT = "kyc_agent"
CHECK_LABELS = {"ytunnus": ("Y-tunnus", "Business ID"), "ytj": ("YTJ", "YTJ register"), "vies": ("ALV-numero (VIES)", "VAT (VIES)"),
                "sanctions": ("Pakoteseulonta", "Sanctions screening"), "ubo": ("Tosiasialliset edunsaajat", "Beneficial owners")}


def _legal_point(company: dict, checks: dict) -> str | None:
    if (checks.get("sanctions") or {}).get("status") == "fail":
        return "sanctions_potential_match"
    countries = {(company.get("address") or {}).get("country"), company.get("country")}
    countries |= {u.get("country") for u in company.get("ubos") or []}
    if countries & {"RU", "BY"}:
        return "ru_by_trade"
    if any(u.get("pep") for u in company.get("ubos") or []):
        return "pep_relationship"
    return None


async def gate_kyc_run(company: dict, checks: dict, risk: dict, green: bool, sanctions_status: str, settings: dict,
                       actor: dict | None) -> Decision:
    """Called by services.kyc._run_kyc with the fresh check results."""
    failing = [k for k, s in risk.items() if s not in ("pass", "not_applicable")]
    extra = [policy_reason(f"check_{k}", f"{CHECK_LABELS.get(k, (k, k))[0]}: {checks[k].get('summary_fi') or checks[k].get('summary')}",
                           f"{CHECK_LABELS.get(k, (k, k))[1]}: {checks[k].get('summary')}") for k in failing]
    if green and not settings.get("auto_approve_low_risk_kyc"):
        extra.append(policy_reason("kyc_policy", "Matalan riskin automaattinen hyväksyntä on pois päältä asetuksista",
                                   "Automatic approval of low-risk customers is off in settings"))
    name = company.get("name") or "—"
    summary_en = ("All risk checks are green." if green else "; ".join(f"{k}: {checks[k].get('summary')}" for k in failing))[:900]
    summary_fi = ("Kaikki riskitarkistukset ovat kunnossa." if green else
                  "; ".join(f"{CHECK_LABELS.get(k, (k, k))[0]}: {checks[k].get('summary_fi') or checks[k].get('summary')}"
                            for k in failing))[:900]
    return await decide(
        AGENT, "approve_customer", subject=("company", company["id"]), subject_label=name, company_id=company["id"],
        inputs={"risk": risk, "checks": {k: {"status": v.get("status"), "summary": v.get("summary"), "source": v.get("source")}
                                         for k, v in checks.items()},
                "sanctions_status": sanctions_status, "sandbox": bool(checks.get("ytj", {}).get("data", {}).get("sandbox"))},
        decision={"kyc_status": "approved" if green else "in_review"}, confidence=None, model=None,
        legal_point=_legal_point(company, checks), extra_reasons=extra, approval_kind="kyc_review",
        title=f"KYC review: {name}", title_fi=f"Asiakastuntemuksen tarkistus: {name}", summary=summary_en,
        summary_fi=summary_fi, link=f"/ops/customers/{company['id']}",
        approval_data={"risk": risk, "sanctions_status": sanctions_status})


def profile_complete(c: dict) -> bool:
    from services.kyc import normalise_business_id
    return bool(normalise_business_id(c.get("business_id")) and (c.get("signatory") or {}).get("name")
                and (c.get("ubos") or c.get("ubo_none_declared")))


@job("kyc.decide", agent=True, max_attempts=4, backoff=(60, 300, 900, 3600), timeout=600,
     label_fi="Asiakastuntemus", label_en="Customer due diligence")
async def kyc_decide(j):
    from services import kyc
    cid = j["payload"]["company_id"]
    c = await db.raw.companies.find_one({"id": cid}, NO_ID)
    if not c:
        return {"skipped": "company not found"}
    if c.get("status") == "blocked" or c.get("kyc_status") == "rejected":
        return {"skipped": "a person decided this customer"}
    if c.get("kyc_status") == "approved" and not c.get("kyc_stale"):
        return {"skipped": "already approved"}
    if c.get("kyc_status") == "pending" and not kyc.stale_pending(c):
        raise JobDeferred(60, "KYC run in progress")
    if not profile_complete(c):
        return {"skipped": "profile incomplete"}
    out = await kyc.run_kyc(cid, agent_bot(AGENT))
    await db.raw.companies.update_one({"id": cid}, {"$set": {"kyc_stale": False}})
    return {"kyc_status": out.get("kyc_status"), "sanctions_status": out.get("sanctions_status")}


@sweeper
async def sweep_kyc():
    """Customers whose profile is complete but whose checks never ran, went stale or stopped mid-way."""
    from services import kyc
    n = 0
    mk = await mode_key()
    q = {"kyc_status": {"$in": ["not_started", "pending", None]}, "business_id": {"$nin": [None, ""]},
         "status": {"$ne": "blocked"}, "demo": {"$ne": True}}
    async for c in db.raw.companies.find({"$or": [q, {"kyc_stale": True, "kyc_status": {"$ne": "rejected"}}]}, NO_ID).limit(50):
        if not profile_complete(c) or (c.get("kyc_status") == "pending" and not kyc.stale_pending(c)):
            continue
        await enqueue("kyc.decide", {"company_id": c["id"]}, company_id=c["id"], subject=("company", c["id"]),
                      key=f"kyc:{c['id']}:{c.get('updated_at') or c.get('created_at')}:{mk}", source="sweep")
        n += 1
    # Green customers parked in the owner queue while Autopilot was off are decided again once it is on.
    if mk == "on":
        async for a in db.raw.approvals.find({"kind": "kyc_review", "status": "pending"}, NO_ID).limit(50):
            c = await db.raw.companies.find_one({"id": a["ref_id"]}, NO_ID)
            if c and (c.get("kyc") or {}).get("green") and c.get("kyc_status") == "in_review":
                await db.raw.companies.update_one({"id": c["id"]}, {"$set": {"kyc_stale": True}})
                await enqueue("kyc.decide", {"company_id": c["id"]}, company_id=c["id"], subject=("company", c["id"]),
                              key=f"kyc:{c['id']}:recheck:{a['id']}:{mk}", source="sweep")
                n += 1
    return n


@on("company.profile_completed")
async def _on_profile(p):
    c = p["company"]
    await enqueue("kyc.decide", {"company_id": c["id"]}, company_id=c["id"], subject=("company", c["id"]),
                  key=f"kyc:{c['id']}:{c.get('updated_at') or now_iso()}:{await mode_key()}")
