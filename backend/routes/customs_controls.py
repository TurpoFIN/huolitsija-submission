"""Customs AEO-style controls API (s11): control settings, per-declaration control status, the AEO audit export
(CSV) and customs compliance KPIs. Importing this module registers the daily retention job."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from core.agent_runtime import agent_bot
from core.approvals import four_eyes_block
from core.audit import audit
from core.db import db
from core.deps import require_internal, require_perm
from core.settings import get_settings, update_settings
from core.util import NO_ID, now_iso
from services import customs_aeo as aeo

router = APIRouter(prefix="/api/customs", tags=["customs-controls"])


async def _settings_view() -> dict:
    s = await get_settings()
    filers = []
    for uid in s.get("customs_authorised_filers") or []:
        u = await db.raw.users.find_one({"id": uid}, {"_id": 0, "id": 1, "name": 1, "email": 1, "role": 1, "disabled": 1})
        filers.append(u or {"id": uid, "missing": True})
    return {**{k: s.get(k) for k in aeo.SETTING_KEYS}, "authorised_filers": filers,
            "eligible_filers": await aeo.authorised_filers(s),
            "customs_filing_delegated": s.get("customs_filing_delegated"),
            "customs_delegation_scope": s.get("customs_delegation_scope"),
            "help": {"fi": "Neljän silmän rajat euroina (raja ylittyy, kun summa on vähintään raja). Tyhjä valtuutettujen "
                           "lista = kaikki customs.file-oikeuden haltijat.",
                     "en": "Four-eyes thresholds in euros (reached when the amount is at least the threshold). An empty "
                           "authorised filers list = everyone with customs.file."}}


@router.get("/controls/settings")
async def get_controls_settings(user: dict = Depends(require_perm("settings.write"))):
    return await _settings_view()


@router.patch("/controls/settings")
async def patch_controls_settings(body: dict, user: dict = Depends(require_perm("settings.write"))):
    clean = await aeo.validate_controls_settings(body)
    before = await get_settings()
    await update_settings(clean)
    changes = {k: {"from": before.get(k), "to": v} for k, v in clean.items() if before.get(k) != v}
    await audit(user, "customs_controls_settings_changed", "settings", "app", {"changes": changes})
    return await _settings_view()


@router.get("/controls/kpis")
async def controls_kpis(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                        user: dict = Depends(require_perm("customs.review"))):
    return await aeo.kpis(date_from, date_to)


@router.get("/controls/audit.csv")
async def controls_audit_csv(date_from: str | None = Query(None, alias="from"),
                             date_to: str | None = Query(None, alias="to"),
                             user: dict = Depends(require_perm("customs.review"))):
    rows = await aeo.audit_rows(date_from, date_to)
    f, t, _lo, _hi = aeo.period(date_from, date_to, default_days=366 * int((await get_settings())["record_retention_years"]))
    await audit(user, "customs_audit_exported", "settings", "customs_audit", {"from": f, "to": t, "rows": len(rows)})
    return Response(content=aeo.rows_to_csv(rows).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="tulli-aeo-audit-{f}-{t}.csv"',
                             "X-Row-Count": str(len(rows))})


@router.get("/{decl_id}/controls")
async def declaration_controls(decl_id: str, kind: str = "import", refresh: bool = False,
                               user: dict = Depends(require_internal)):
    """Control status of one declaration: may you file it, may Autopilot, does it need four eyes and who can approve."""
    if kind not in aeo.KINDS:
        raise HTTPException(400, "kind is import, export, transit or ens")
    decl = await db.raw[aeo.KINDS[kind]["collection"]].find_one({"id": decl_id}, NO_ID)
    if not decl:
        raise HTTPException(404, "Declaration not found")
    approval = await db.raw.approvals.find_one({"id": decl["approval_id"]}, NO_ID) if decl.get("approval_id") else None
    you = await aeo.filing_controls(decl, user, kind=kind, record=False, stage="check", approval=approval,
                                    refresh=refresh)
    bot = await aeo.filing_controls(decl, agent_bot("customs_agent"), kind=kind, record=False, stage="check",
                                    approval=approval, refresh=False)
    filers = await aeo.authorised_filers()
    excluded = set(you["four_eyes"]["excluded_ids"])
    ap = None
    if approval:
        ap = {k: approval.get(k) for k in ("id", "status", "requested_by", "decided_by", "decided_at", "four_eyes_override",
                                           "reasons", "void_reason", "void_reason_fi")}
        ap["four_eyes_for_you"] = await four_eyes_block(approval, user) if approval.get("status") == "pending" else None
    return {"declaration_id": decl_id, "lrn": decl.get("lrn"), "status": decl.get("status"), "kind": kind,
            "checked_at": now_iso(), "you": you,
            "autopilot": {"may_file": bot["may_file"], "reasons": aeo.agent_reasons(bot),
                          "legal_points": [r.get("legal_point") for r in bot["reasons"] if r.get("legal_point")]},
            "four_eyes": you["four_eyes"], "approval": ap,
            "eligible_approvers": [u for u in filers if u["id"] not in excluded],
            "retention": decl.get("retention")}
