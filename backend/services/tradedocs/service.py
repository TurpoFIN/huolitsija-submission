"""Issue, number, version and amend trade documents (collection `trade_documents`, one row per version).

    doc = await issue(shipment_id, "house_bl", actor=user)                 # first version (or the current one: idempotent)
    doc = await amend("HUOB26000012", {"consignee.name": "…"}, "Consignee name corrected", actor=user)
    pdf = await pdf_bytes(doc["id"], "fi")

Every version keeps the full data snapshot it was printed from, the PDFs in Finnish and English (GridFS), their
SHA-256 and the structured exports, so any version can be shown again exactly as it was sent. An amendment keeps the
document number, adds a version, marks the previous one superseded and records each changed field (old -> new) with
the reason in the row and in the audit log.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from pymongo.errors import DuplicateKeyError

from fastapi import HTTPException

from core.audit import actor_ref, audit
from core.db import db, register_index
from core.events import emit
from core.refs import next_ref, next_seq
from core.storage import get_file, put_file
from core.util import NO_ID, new_id, now_iso
from services.tradedocs import catalog, data, pdf, structured

log = logging.getLogger("tradedocs")

register_index("trade_documents", "id", unique=True)
register_index("trade_documents", [("doc_no", 1), ("version", 1)], unique=True)
register_index("trade_documents", [("shipment_id", 1), ("kind", 1), ("current", 1)])
register_index("trade_documents", [("company_id", 1), ("created_at", -1)])

LANGS = ("fi", "en")
# Fields an amendment may not change: identity of the document and of the shipment.
FROZEN = ("shipment.id", "shipment.reference", "shipment.company_id", "issuer", "customer", "dates", "source_document_id")



def validate_final(kind: str, ctx: dict):
    """Refuse an incomplete final document; drafts keep missing source values visible."""
    missing = []
    for party in ("shipper", "consignee"):
        if not (ctx.get(party) or {}).get("name"):
            missing.append(party + " name")
    if kind in ("commercial_invoice", "packing_list", "house_bl", "hawb", "cmr", "certificate_of_origin", "eur1_application", "arrival_notice", "delivery_order"):
        if not ctx.get("goods") or any(not g.get("description") for g in ctx["goods"]):
            missing.append("goods description")
    if kind == "commercial_invoice":
        for i, g in enumerate(ctx["goods"], 1):
            for field in ("quantity", "unit", "unit_price", "amount", "currency"):
                if g.get(field) is None or g.get(field) == "":
                    missing.append(f"line {i} {field.replace('_', ' ')}")
        if len({g.get("currency") for g in ctx["goods"]}) != 1:
            missing.append("one consistent invoice currency")
    if kind in ("packing_list", "house_bl", "hawb", "cmr", "arrival_notice", "delivery_order"):
        for field in ("packages", "gross_kg"):
            if ctx["totals"].get(field) is None:
                missing.append(field.replace('_', ' '))
    if kind == "arrival_notice":
        main = ctx.get("main_leg") or {}
        shipment = ctx.get("shipment") or {}
        if not (main.get("ata") or shipment.get("ata") or main.get("eta") or shipment.get("eta")):
            missing.append("arrival date or ETA")
        if not (ctx.get("port_of_discharge") or {}).get("name"):
            missing.append("arrival terminal")
    if kind in ("certificate_of_origin", "eur1_application"):
        if any(not g.get("origin_country") for g in ctx.get("goods", [])):
            missing.append("documented country of origin for each goods line")
    if kind == "dg_declaration":
        entries = ctx.get("dangerous_goods") or []
        if not entries:
            missing.append("dangerous goods details")
        for i, entry in enumerate(entries, 1):
            for field in ("un_number", "proper_shipping_name", "class", "packages", "package_type"):
                if entry.get(field) is None or not str(entry[field]).strip():
                    missing.append(f"DG line {i} {field.replace('_', ' ')}")
            if entry.get("gross_kg") is None and entry.get("net_kg") is None:
                missing.append(f"DG line {i} gross or net mass")
    if missing:
        raise HTTPException(409, "Final document needs source data: " + "; ".join(missing) + ". Generate a draft or correct the shipment/source document.")

async def next_number(kind: str) -> str:
    spec = catalog.KINDS[kind]
    if kind in ("house_bl", "hawb", "cmr"):
        yy = datetime.now(timezone.utc).strftime("%y")
        n = await next_seq(f"tradedoc:{spec['prefix']}:{yy}")
        return f"{spec['prefix']}{yy}{n:06d}"  # 12 characters: fits DCSA transportDocumentReference and the IATA HWB number
    return await next_ref(spec["prefix"])


def public(doc: dict | None) -> dict | None:
    if not doc:
        return None
    out = {k: v for k, v in doc.items() if k not in ("_id",)}
    out["title"] = catalog.KINDS[doc["kind"]]["title"]
    out["formats"] = catalog.KINDS[doc["kind"]]["formats"]
    out["draft_only"] = bool(catalog.KINDS[doc["kind"]].get("draft_only"))
    return out


def _digest(ctx: dict) -> str:
    body = {k: v for k, v in ctx.items() if k != "dates"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


async def _shipment(shipment_id: str) -> dict:
    s = await db.raw.shipments.find_one({"id": shipment_id}, NO_ID) if hasattr(db, "raw") else None
    s = s or await db.shipments.find_one({"id": shipment_id}, NO_ID)
    if not s:
        raise HTTPException(404, "Shipment not found")
    return s


async def current(shipment_id: str, kind: str) -> dict | None:
    return await db.trade_documents.find_one({"shipment_id": shipment_id, "kind": kind, "current": True}, NO_ID)


async def _render_and_store(kind: str, ctx: dict, meta: dict) -> tuple[dict, dict, dict]:
    files, sha = {}, {}
    for lang in LANGS:
        blob = pdf.render(kind, ctx, lang=lang, meta=meta)
        name = f"{meta['doc_no']}-v{meta['version']}-{lang}.pdf"
        files[f"pdf_{lang}"] = await put_file(blob, name, "application/pdf",
                                              {"company_id": ctx["shipment"]["company_id"], "shipment_id": ctx["shipment"]["id"],
                                               "trade_doc": meta["doc_no"], "version": meta["version"], "lang": lang})
        sha[lang] = hashlib.sha256(blob).hexdigest()
    exports = {}
    for fmt in catalog.KINDS[kind]["formats"]:
        if fmt != "json":  # the json export is the stored snapshot itself (see export())
            exports[fmt] = structured.build(kind, fmt, ctx, meta)
    return files, sha, exports


async def _write_version(*, kind: str, ctx: dict, doc_no: str, version: int, status: str, actor, previous: dict | None,
                         amendment: dict | None, source: str) -> dict:
    at = now_iso()
    first_issue = ((previous or {}).get("data") or {}).get("dates", {}).get("issue")
    ctx["dates"]["issue"] = (first_issue or at) if amendment else at
    if amendment:
        ctx["dates"]["amended"] = at
    meta = {"doc_no": doc_no, "version": version, "status": status, "issued_at": at, "kind": kind,
            "amendment": amendment, "first_issued_at": (previous or {}).get("first_issued_at") or at}
    files, sha, exports = await _render_and_store(kind, ctx, meta)
    row = {"id": new_id(), "doc_no": doc_no, "kind": kind, "version": version, "current": True, "status": status,
           "shipment_id": ctx["shipment"]["id"], "shipment_reference": ctx["shipment"]["reference"],
           "company_id": ctx["shipment"]["company_id"], "data": ctx, "digest": _digest(ctx), "structured": exports,
           "files": files, "sha256": sha, "issued_at": at, "first_issued_at": meta["first_issued_at"],
           "issued_by": actor_ref(actor), "amendment": amendment, "supersedes": (previous or {}).get("id"),
           "dispatches": [], "source": source, "created_at": at}
    if previous:
        await db.trade_documents.update_one({"id": previous["id"]}, {"$set": {"current": False, "status": "superseded",
                                                                              "superseded_at": at, "superseded_by": row["id"]}})
    await db.trade_documents.insert_one(dict(row))
    return row


async def _issue(shipment_id: str, kind: str, *, actor: dict | None = None, final: bool = True, source: str = "manual",
                overrides: dict | None = None) -> dict:
    """Issue `kind` for the shipment. Idempotent: an existing current version is returned as is (a draft is promoted
    to a final version when `final`). Raises 409 with the reason when the document does not apply to the shipment."""
    if kind not in catalog.KINDS:
        raise HTTPException(400, f"Unknown document kind '{kind}'.")
    if catalog.KINDS[kind].get("draft_only"):
        final = False
    s = await _shipment(shipment_id)
    if s.get("status") == "cancelled":
        raise HTTPException(409, "The shipment is cancelled.")
    cur = await current(shipment_id, kind)
    if cur and (cur["status"] == "issued" or not final):
        if kind == "delivery_order" and cur["status"] == "issued":
            from services.tradedocs.release import validate
            await validate(cur["data"])
        return cur
    fresh = await data.build_context(s, kind=kind)
    # Finalising a reviewed draft must retain its corrections. Refresh is the explicit
    # action for replacing documentary data from the shipment. Operational timestamps
    # still advance when a transport document becomes due at pickup/departure.
    ctx = copy.deepcopy(cur["data"]) if cur and cur.get("amendment") else fresh
    if ctx is not fresh:
        ctx["dates"].update({k: fresh["dates"][k] for k in ("shipped_on_board", "taking_over")})
        ctx["shipment"]["status"] = fresh["shipment"]["status"]
    if kind == "delivery_order":
        ctx["delivery_release"] = fresh.get("delivery_release")
    ok, why = catalog.applies(kind, ctx)
    if not ok:
        raise HTTPException(409, why)
    for path, value in (overrides or {}).items():
        data.set_path(ctx, path, value)
    if final:
        validate_final(kind, ctx)
        if kind == "delivery_order":
            from services.tradedocs.release import validate
            await validate(ctx)
    status = "issued" if final else "draft"
    if cur:  # draft -> final: same number, next version
        row = await _write_version(kind=kind, ctx=ctx, doc_no=cur["doc_no"], version=cur["version"] + 1, status=status,
                                   actor=actor, previous=cur, amendment=None, source=source)
    else:
        doc_no = await next_number(kind)
        row = await _write_version(kind=kind, ctx=ctx, doc_no=doc_no, version=1, status=status, actor=actor, previous=None,
                                   amendment=None, source=source)
    await audit(actor, f"tradedoc.{status}", "trade_document", row["id"],
                {"doc_no": row["doc_no"], "kind": kind, "version": row["version"], "shipment": s.get("reference"),
                 "sha256": row["sha256"], "source": source}, company_id=s.get("company_id"))
    await emit("tradedoc.issued", {"document": public(row), "actor": actor})
    return row


async def _amend(doc_no: str, changes: dict, reason: str, *, actor: dict | None, from_source: bool = False) -> dict:
    """New version with `changes` ({dotted path: new value}) applied to the current version's data."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise HTTPException(400, "Give the reason for the amendment.")
    if not changes:
        raise HTTPException(400, "Nothing to change.")
    cur = await db.trade_documents.find_one({"doc_no": doc_no, "current": True}, NO_ID)
    if not cur:
        raise HTTPException(404, "Document not found")
    if cur["status"] == "cancelled":
        raise HTTPException(409, "The document is cancelled; issue a new one.")
    ctx = copy.deepcopy(cur["data"])
    diff = []
    for path, value in changes.items():
        if cur["kind"] == "delivery_order" and (path == "delivery_release" or path.startswith("delivery_release.")) and not from_source:
            raise HTTPException(400, "Record release evidence on the shipment; it cannot be amended on the order")
        if any(path == f or path.startswith(f + ".") or f.startswith(path + ".") for f in FROZEN) and not (from_source and path == "source_document_id"):
            raise HTTPException(400, f"{path} cannot be amended.")
        old = data.get_path(ctx, path)
        if old == value:
            continue
        source_field = from_source and (path == "source_document_id" or (cur["kind"] == "delivery_order" and path == "delivery_release"))
        if (path.split(".")[0] not in ctx and not source_field) or any(p.startswith("$") or p.startswith("_") for p in path.split(".")):
            raise HTTPException(400, f"Unknown amendment field: {path}")
        try:
            data.set_path(ctx, path, value)
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            raise HTTPException(400, f"Invalid amendment field: {path}")
        diff.append({"path": path, "old": old, "new": value})
    if not diff:
        raise HTTPException(409, "The values are already on the document.")
    if not from_source and any(path == "goods" or path.startswith("goods.") for path in changes):
        for key, field in (("amount", "amount"), ("net_kg", "net_kg"), ("gross_kg", "gross_kg"), ("packages", "packages")):
            rows = ctx.get("goods") or []
            previous_rows = cur["data"].get("goods") or []
            if [g.get(field) for g in rows] == [g.get(field) for g in previous_rows]:
                continue  # a description correction must retain known consignment totals
            total = sum(g[field] for g in rows) if rows and all(isinstance(g.get(field), (int, float)) for g in rows) else None
            old = ctx["totals"].get(key)
            if total != old:
                ctx["totals"][key] = total
                diff.append({"path": "totals." + key, "old": old, "new": total})
    ok, why = catalog.applies(cur["kind"], ctx)
    if not ok:
        raise HTTPException(409, why)
    if cur["status"] == "issued":
        validate_final(cur["kind"], ctx)
        if cur["kind"] == "delivery_order":
            from services.tradedocs.release import validate
            await validate(ctx)
    n_amend = int(((cur.get("amendment") or {}).get("number") or 0)) + 1
    amendment = {"number": n_amend, "reason": reason, "changes": diff, "requested_by": actor_ref(actor), "at": now_iso()}
    if cur["kind"] == "house_bl" and cur["status"] == "issued" and (cur["data"].get("negotiable") or False):
        amendment["surrender_required"] = True  # paper originals of a negotiable B/L are surrendered before a switch
    row = await _write_version(kind=cur["kind"], ctx=ctx, doc_no=doc_no, version=cur["version"] + 1, status=cur["status"],
                               actor=actor, previous=cur, amendment=amendment, source="amendment")
    await audit(actor, "tradedoc.amended", "trade_document", row["id"],
                {"doc_no": doc_no, "kind": cur["kind"], "version": row["version"], "previous_version": cur["version"],
                 "reason": reason, "changes": diff}, company_id=cur.get("company_id"))
    await emit("tradedoc.amended", {"document": public(row), "previous": public(cur), "actor": actor})
    return row


async def refresh(doc_no: str, *, actor: dict | None, reason: str) -> dict | None:
    """Amend from the shipment's current data (vessel, ETA, parties changed). None when nothing changed."""
    cur = await db.trade_documents.find_one({"doc_no": doc_no, "current": True}, NO_ID)
    if not cur:
        raise HTTPException(404, "Document not found")
    s = await _shipment(cur["shipment_id"])
    fresh = await data.build_context(s, kind=cur["kind"])
    changes = {}
    if cur["kind"] == "delivery_order" and fresh.get("delivery_release") != cur["data"].get("delivery_release"):
        changes["delivery_release"] = fresh.get("delivery_release")
    for key in ("shipper", "consignee", "notify", "main_leg", "port_of_loading", "port_of_discharge", "place_of_delivery",
                "goods", "totals", "containers", "dangerous_goods", "refs", "delivery_carrier", "source_document_id"):
        if fresh.get(key) != cur["data"].get(key):
            changes[key] = fresh.get(key)
    if not changes:
        return None
    async with _version_lock(cur["shipment_id"], cur["kind"]):
        return await _amend(doc_no, changes, reason, actor=actor, from_source=True)


async def cancel(doc_no: str, reason: str, *, actor: dict | None) -> dict:
    cur = await db.trade_documents.find_one({"doc_no": doc_no, "current": True}, NO_ID)
    if not cur:
        raise HTTPException(404, "Document not found")
    await db.trade_documents.update_one({"id": cur["id"]}, {"$set": {"status": "cancelled", "cancelled_at": now_iso(),
                                                                     "cancel_reason": reason, "cancelled_by": actor_ref(actor)}})
    await audit(actor, "tradedoc.cancelled", "trade_document", cur["id"], {"doc_no": doc_no, "reason": reason},
                company_id=cur.get("company_id"))
    return await db.trade_documents.find_one({"id": cur["id"]}, NO_ID)


async def get(doc_id: str) -> dict | None:
    return await db.trade_documents.find_one({"id": doc_id}, NO_ID)


async def versions(doc_no: str) -> list[dict]:
    return await db.trade_documents.find({"doc_no": doc_no}, {"_id": 0, "data": 0, "structured": 0}).sort("version", 1).to_list(100)


async def list_for_shipment(shipment_id: str, *, include_superseded: bool = False) -> list[dict]:
    q = {"shipment_id": shipment_id}
    if not include_superseded:
        q["current"] = True
    rows = await db.trade_documents.find(q, {"_id": 0, "data": 0, "structured": 0}).to_list(500)
    order = {k: i for i, k in enumerate(catalog.ORDER)}
    rows.sort(key=lambda r: (order.get(r["kind"], 99), -r["version"]))
    return [public(r) for r in rows]


async def pdf_bytes(doc_id: str, lang: str = "fi") -> tuple[bytes, str]:
    doc = await get(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    lang = lang if lang in LANGS else "fi"
    blob, _name, _ct = await get_file(doc["files"][f"pdf_{lang}"])
    return blob, f"{doc['doc_no']}-v{doc['version']}-{lang}.pdf"


async def export(doc_id: str, fmt: str):
    doc = await get(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if fmt == "json":
        return structured.plain(doc["kind"], doc["data"], {"doc_no": doc["doc_no"], "version": doc["version"], "status": doc["status"],
                                                           "issued_at": doc["issued_at"], "amendment": doc.get("amendment")})
    if fmt not in (doc.get("structured") or {}):
        raise HTTPException(404, f"No {fmt} export for this document.")
    return doc["structured"][fmt]


def suggestions(ctx: dict) -> list[dict]:
    """Every kind with whether it applies to the shipment (for the UI's 'create document' menu)."""
    out = []
    for kind in catalog.ORDER:
        ok, why = catalog.applies(kind, ctx)
        out.append({"kind": kind, "title": catalog.KINDS[kind]["title"], "applies": ok, "reason": why})
    return out


@asynccontextmanager
async def _version_lock(shipment_id: str, kind: str):
    """One writer across workers/API requests. An interrupted writer's lease expires."""
    owner, at = new_id(), datetime.now(timezone.utc)
    key = f"{shipment_id}:{kind}"
    try:
        await db.trade_document_locks.update_one(
            {"_id": key, "$or": [{"until": {"$lte": at}}, {"until": {"$exists": False}}]},
            {"$set": {"owner": owner, "until": at + timedelta(minutes=5)}}, upsert=True)
    except DuplicateKeyError:
        raise HTTPException(409, "This document is being updated. Retry shortly.")
    try:
        yield
    finally:
        await db.trade_document_locks.delete_one({"_id": key, "owner": owner})


async def issue(shipment_id: str, kind: str, **kwargs) -> dict:
    async with _version_lock(shipment_id, kind):
        return await _issue(shipment_id, kind, **kwargs)


async def amend(doc_no: str, changes: dict, reason: str, *, actor: dict | None) -> dict:
    cur = await db.trade_documents.find_one({"doc_no": doc_no, "current": True}, NO_ID)
    if not cur:
        raise HTTPException(404, "Document not found")
    async with _version_lock(cur["shipment_id"], cur["kind"]):
        return await _amend(doc_no, changes, reason, actor=actor)
