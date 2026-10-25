"""Authenticated local demo authority. No real-authority forwarding exists here."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from core.deps import require_internal
from services.demo_world import authority, boundary
from services.tulli_transport import TransportError

router = APIRouter(prefix="/api/demo/world", tags=["demo"])


@router.post("/authority/tulli")
async def tulli(request: Request, user: dict = Depends(require_internal)):
    if not boundary.on():
        raise HTTPException(404, "Not found")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 24 * 1024 * 1024:
            raise HTTPException(413, "Authority request exceeds size limit")
    try:
        return Response(await authority.handle(bytes(raw)), media_type="application/soap+xml")
    except (TransportError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
