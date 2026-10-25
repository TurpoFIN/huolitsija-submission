"""s7 tenders routes (see shared/interfaces.md, section s7). Internal, quotes.write; the filled file also opens with a
signed link (?t=) so an e-mailed customer can download it without an account."""
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core.db import db
from core.deps import get_optional_user, require_perm
from core.util import NO_ID
from core.web import base_url
from services import tenders as svc

router = APIRouter(prefix="/api", tags=["tenders"])


class ReturnIn(BaseModel):
    to: str | None = None


class AwardIn(BaseModel):
    lane_ids: list[str] | None = None


class LostIn(BaseModel):
    reason: str | None = None


def _flag(v) -> bool:
    return str(v or "").lower() in ("1", "true", "yes", "on")


@router.post("/tenders")
async def create_tender(request: Request, user: dict = Depends(require_perm("quotes.write"))):
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(400, "Liitä tiedosto kenttään file / Attach the sheet as the form field 'file'.")
        data = await f.read(svc.MAX_BYTES + 1)
        meta = {k: (form.get(k) or None) for k in ("lead_id", "opportunity_id", "name", "requester_email")}
        t = await svc.create_from_file(data, f.filename or "tender.xlsx", source="upload", company_id=form.get("company_id") or None,
                                       actor=user, meta=meta)
        price = _flag(form.get("price")) or _flag(request.query_params.get("price"))
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Lähetä multipart-tiedosto tai JSON {lanes:[...]} / Send a multipart file or JSON "
                                     "{lanes:[...]}.") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON body must be an object {lanes:[...]}.")
        meta = {k: body.get(k) for k in ("lead_id", "opportunity_id", "name", "requester_email")}
        t = await svc.create_from_lanes(body.get("lanes"), company_id=body.get("company_id"), actor=user, meta=meta)
        price = bool(body.get("price")) or _flag(request.query_params.get("price"))
    if price:
        t = await svc.price_tender(t["id"], user)
    return {"tender": t}


@router.post("/tenders/inbound-email")
async def inbound_email(request: Request, user: dict = Depends(require_perm("quotes.write"))):
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "Tyhjä viesti / Empty message: send the raw RFC 822 message (message/rfc822).")
    if len(raw) > svc.MAX_BYTES + 1024 * 1024:
        raise HTTPException(413, "Viesti on liian suuri / The message is too large.")
    items = await svc.create_from_email(raw)
    return {"tenders": items, "count": len(items)}


@router.get("/tenders")
async def list_tenders(status: str | None = None, company_id: str | None = None, source: str | None = None, limit: int = 100,
                       user: dict = Depends(require_perm("quotes.write"))):
    q = {}
    if status and status != "all":
        q["status"] = status
    if company_id:
        q["company_id"] = company_id
    if source:
        q["source"] = source
    docs = await db.tenders.find(q, NO_ID).sort("created_at", -1).to_list(min(max(limit, 1), 500))
    counts = {s: 0 for s in svc.STATUSES}
    async for row in db.tenders.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    return {"items": [svc.summary(d) for d in docs], "total": len(docs), "counts": counts, "engine": svc.engine_name()}


@router.get("/tenders/{tender_id}")
async def get_tender(tender_id: str, user: dict = Depends(require_perm("quotes.write"))):
    return {"tender": await svc.load(tender_id)}


@router.post("/tenders/{tender_id}/price")
async def price(tender_id: str, user: dict = Depends(require_perm("quotes.write"))):
    return {"tender": await svc.price_tender(tender_id, user)}


@router.get("/tenders/{tender_id}/file")
async def tender_file(tender_id: str, t: str | None = None, user: dict | None = Depends(get_optional_user)):
    if not (user and "quotes.write" in (user.get("permissions") or [])) and not (t and svc.check_file_token(tender_id, t)):
        raise HTTPException(401 if not user else 403, "Kirjaudu sisään tai käytä sähköpostin linkkiä / Sign in or use the "
                                                      "link from the e-mail.")
    tender = await svc.load(tender_id)
    data, fname = await svc.output_bytes(tender)
    return Response(data, media_type=svc.XLSX_TYPE, headers={
        "Content-Disposition": f"attachment; filename=\"{fname.encode('ascii', 'replace').decode()}\"; "
                               f"filename*=UTF-8''{urlquote(fname)}", "Cache-Control": "no-store"})


@router.get("/tenders/{tender_id}/original")
async def original_file(tender_id: str, user: dict = Depends(require_perm("quotes.write"))):
    from core.storage import get_file

    tender = await svc.load(tender_id)
    doc = await db.documents.find_one({"id": tender.get("original_document_id")}, NO_ID) if tender.get("original_document_id") else None
    if not doc:
        raise HTTPException(404, "Alkuperäistä tiedostoa ei ole / No original file (JSON tender).")
    data, fname, ctype = await get_file(doc["file_id"])
    return Response(data, media_type=ctype, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urlquote(fname)}"})


@router.post("/tenders/{tender_id}/return")
async def return_tender(tender_id: str, request: Request, body: ReturnIn | None = None,
                        user: dict = Depends(require_perm("quotes.write"))):
    return {"tender": await svc.return_tender(tender_id, user, to=(body.to if body else None), base=base_url(request))}


@router.post("/tenders/{tender_id}/award")
async def award(tender_id: str, body: AwardIn | None = None, user: dict = Depends(require_perm("quotes.write"))):
    return await svc.award_tender(tender_id, body.lane_ids if body else None, user)


@router.post("/tenders/{tender_id}/lost")
async def lost(tender_id: str, body: LostIn | None = None, user: dict = Depends(require_perm("quotes.write"))):
    return {"tender": await svc.mark_lost(tender_id, body.reason if body else None, user)}
