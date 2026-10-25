"""Manually reviewed carrier/terminal release evidence. Never an automatic issuer verification."""
from datetime import datetime, timezone
from hashlib import sha256
from gridfs.errors import NoFile

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.audit import actor_ref, audit
from core.db import db
from core.storage import get_file
from core.util import NO_ID, new_id, now_iso


class ReleaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    document_id: str = Field(min_length=1, max_length=100)
    reference: str = Field(min_length=3, max_length=150)
    issuer: str = Field(min_length=3, max_length=150)
    terminal: str = Field(min_length=3, max_length=200)
    haulier: str = Field(min_length=3, max_length=150)
    transport_document: str = Field(min_length=3, max_length=150)
    valid_until: datetime
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("valid_until")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Include the timezone in the release expiry")
        return value


async def record(shipment_id, body: ReleaseIn, actor):
    from services.tradedocs import service
    if not actor or actor.get("org_type") != "internal":
        raise HTTPException(403, "An operations user must review the release document")
    async with service._version_lock(shipment_id, "delivery_order"):
        shipment = await service._shipment(shipment_id)
        if shipment.get("status") == "cancelled":
            raise HTTPException(409, "The shipment is cancelled")
        if body.valid_until <= datetime.now(timezone.utc):
            raise HTTPException(409, "The release authorization has expired")
        doc = await db.documents.find_one({"id": body.document_id, "shipment_id": shipment_id,
                                          "company_id": shipment.get("company_id")}, NO_ID)
        if not doc or not doc.get("file_id") or not doc.get("sha256"):
            raise HTTPException(409, "Upload the carrier or terminal release document to this shipment first")
        blob = await _source_bytes(doc["file_id"])
        if sha256(blob).hexdigest() != doc["sha256"]:
            raise HTTPException(409, "The uploaded release document checksum does not match")
        row = {**body.model_dump(mode="json"), "id": new_id(), "source": "manual",
               "document_sha256": doc["sha256"], "recorded_at": now_iso(), "recorded_by": actor_ref(actor),
               "demo": bool(shipment.get("demo") or shipment.get("test")), "issuer_verified_by_provider": False}
        await db.raw.shipments.update_one({"id": shipment_id},
                                         {"$set": {"delivery_release": row}, "$push": {"delivery_release_history": row}})
        await audit(actor, "tradedoc.release_recorded", "shipment", shipment_id, row,
                    company_id=shipment.get("company_id"))
        return row


async def validate(ctx):
    """Recheck current source at issue, amendment and dispatch, including old generated orders."""
    from services.tradedocs import service, data
    shipment = await service._shipment(ctx["shipment"]["id"])
    row = ctx.get("delivery_release") or {}
    if not row.get("id") or row != shipment.get("delivery_release"):
        raise HTTPException(409, "Delivery order needs current reviewed carrier/terminal release evidence. Record it and generate or refresh the draft.")
    if shipment.get("status") == "cancelled" or shipment.get("holds") or shipment.get("compliance_hold"):
        raise HTTPException(409, "Delivery order is blocked by shipment cancellation or an active hold")
    try:
        expiry = datetime.fromisoformat(row["valid_until"].replace("Z", "+00:00"))
        current = expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
    except (KeyError, ValueError, TypeError):
        current = False
    if not current:
        raise HTTPException(409, "The release authorization has expired or has no valid expiry")
    doc = await db.documents.find_one({"id": row.get("document_id"), "shipment_id": shipment["id"],
                                      "company_id": shipment.get("company_id"), "sha256": row.get("document_sha256")}, NO_ID)
    if not doc or not doc.get("file_id"):
        raise HTTPException(409, "The release source document is missing or changed")
    blob = await _source_bytes(doc["file_id"])
    if sha256(blob).hexdigest() != row["document_sha256"]:
        raise HTTPException(409, "The release source file checksum does not match")
    customs = await data._customs(shipment)
    if customs.get("required") and (customs.get("status") not in ("released", "closed") or not customs.get("mrn") or not customs.get("released_at")):
        raise HTTPException(409, "Customs release with MRN and release date is required before the delivery order")


async def _source_bytes(file_id):
    try:
        blob, _, _ = await get_file(file_id)
        return blob
    except NoFile:
        raise HTTPException(409, "The uploaded release source file is missing")
