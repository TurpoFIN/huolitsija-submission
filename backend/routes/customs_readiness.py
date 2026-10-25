"""Customs authority channels: live Tulli/EU state, missing company registrations, v1.21 envelope self-test
(registered before /customs/{id}). Read-only; nothing is sent to an authority."""
import asyncio
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from core.db import db
from core.deps import require_internal
from services import customs_readiness as ready
from services import customs_test_messages

router = APIRouter(prefix="/api/customs/readiness", tags=["customs"])
NO_ID = {"_id": 0}
# Tulli's published example business messages, deployed next to the official XSDs
EXAMPLES = {"FI415B": ("IMP", Path(ready.wire.SCHEMAS).parents[1] / "import/2.1.7/examples/FI415B.xml"),
            "FI515A": ("EXP", Path(ready.wire.SCHEMAS).parents[1] / "export/1.1.1/examples/FI515A.xml")}


@router.get("")
async def readiness(fresh: bool = False, declaration_id: str | None = None, export_id: str | None = None,
                    example: Literal["FI415B", "FI515A"] | None = None, user: dict = Depends(require_internal)):
    out = await ready.readiness(fresh=fresh)
    tests = []
    if example:
        application, path = EXAMPLES[example]
        res = await asyncio.to_thread(ready.envelope_self_test, path.read_bytes(), application, example)
        tests.append({"payload_source": f"Tulli's published {example} example ({path.relative_to(path.parents[3])})", **res})
    for ident, coll, system, application, message in (
            (declaration_id, db.customs_declarations, "import", "IMP", "FI415B"),
            (export_id, db.export_declarations, "export", "EXP", "FI515A")):
        if not ident:
            continue
        decl = await coll.find_one({"id": ident}, NO_ID)
        if not decl:
            raise HTTPException(404, {"fi": "Ilmoitusta ei löydy", "en": "Declaration not found"})
        from services.customs_messages import tulli_export, tulli_import
        builder = tulli_import if system == "import" else tulli_export
        try:
            payload = builder.build_message(decl, "TEST").encode()
        except ValueError as exc:  # the official business schema refuses this declaration: say so, never wrap it
            tests.append({"declaration_id": ident, "lrn": decl.get("lrn"), "business_message": message, "ok": False,
                          "business_xml_valid": False, "error": str(exc)[:800], "sent": False})
            continue
        res = await asyncio.to_thread(ready.envelope_self_test, payload, application, message)
        tests.append({"declaration_id": ident, "lrn": decl.get("lrn"), "business_xml_valid": True, **res})
    if tests:
        out["envelope_self_tests"] = tests
    return out


@router.get("/test-messages")
async def test_messages(shipment_id: list[str] = Query(..., max_length=4), user: dict = Depends(require_internal)):
    """Per system (import, export, transit, ICS2) for a test tenant's TEST shipment: build the TEST business message,
    validate it against the official schema, wrap it in its transport envelope and run the read-only EOS EORI check.
    Nothing is stored or sent."""
    runs = []
    for ident in shipment_id:
        try:
            runs.append(await customs_test_messages.run(ident))
        except LookupError:
            raise HTTPException(404, {"fi": "Lähetystä ei löydy", "en": "Shipment not found"})
        except PermissionError as exc:
            raise HTTPException(403, {"fi": "Vain testiasiakkaan TEST-lähetys", "en": str(exc)})
    systems = [s for r in runs for s in r["systems"]]
    return {"checked_at": runs[0]["checked_at"], "runs": runs, "sent": False,
            "summary": {s["system"]: {"shipment": s["shipment_ref"], "message": s.get("business_message"),
                                      "schema_valid": s.get("business_xml_valid"),
                                      "envelope_valid": (s.get("envelope") or {}).get("ok"),
                                      "eos": [(e["eori"], e["status"]) for e in (s.get("eos_check") or {}).get("results", [])],
                                      "ok": s.get("ok"), "sent": False} for s in systems}}
