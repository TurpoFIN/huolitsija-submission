"""Shipper REST API v1 (`/api/v1`): machine access for a customer's own systems with an API key.

Every handler authenticates the key (core.api_keys.require_scope), then calls the portal's own route logic as a
synthetic customer user of the key's company, so pricing, booking checks, scoping and customer views (no buy prices
or margins) are exactly the portal's. Mutating calls write an `api_*` audit row with the key id.

Docs: GET /api/v1/openapi.json (this module's routers only) and GET /api/v1/docs (Redoc page).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.api_keys import SCOPES, ApiContext, require_scope
from core.audit import audit
from core.db import db
from core.util import NO_ID
from routes import documents as documents_routes
from routes import invoices as invoices_routes
from routes import quotes as quotes_routes
from routes import shipments as shipments_routes
from services import webhooks_out

API_DESCRIPTION = """
Huolitsija is a Finnish digital freight forwarder. This API lets your ERP, WMS or TMS request quotes, book
shipments, follow them, upload documents and read invoices, and receive signed webhooks when anything changes.

**Authentication.** Create a key in the customer portal (*Rajapinta / API*), choose its scopes, and send it as
`Authorization: Bearer huo_live_…` (or `X-API-Key: huo_live_…`). The key is shown once; we store only its SHA-256.
Keys of test companies start with `huo_test_`.

**Scopes.** quotes:read, quotes:write, bookings:write, shipments:read, tracking:read, documents:write,
invoices:read, webhooks:manage. A call without the scope returns 403.

**Rate limits.** 120 requests per minute per key unless agreed otherwise. Every response carries
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (Unix time); over the limit you get 429 with
`Retry-After` seconds.

**Errors.** JSON `{"detail": "plain sentence"}` with 400 (bad input), 401 (missing/invalid key), 403 (scope),
404, 409 (state conflict, e.g. company verification pending), 429, 5xx.

**Money.** Integer cents in EUR (`total_cents`). Dates ISO 8601 UTC.

**Webhooks.** Register an HTTPS endpoint with `POST /api/v1/webhooks`. We POST
`{"id", "type", "created_at", "data"}` with headers `Huolitsija-Event`, `Huolitsija-Delivery` and
`Huolitsija-Signature: t=<unix>,v1=<hex>` where `v1 = HMAC-SHA256(secret, "<t>.<raw body>")`. Verify it with the
endpoint secret (shown once) and reject `t` older than 5 minutes. Non-2xx answers retry after 1 min, 5 min, 30 min,
2 h and 12 h. Events: quote.created, quote.accepted, shipment.created, shipment.status_changed, shipment.delivered,
invoice.created, invoice.paid.

**EDI.** EDIFACT IFTMIN/IFTSTA/IFTMCS and DCSA JSON are available over SFTP or AS2; ask ops@huolitsija.fi.
"""

router = APIRouter(prefix="/api/v1")
docs_router = APIRouter(prefix="/api/v1", include_in_schema=False)

PLACE_EXAMPLE = "FIHEL"


# ------------------------------------------------------------------------------------------------ models
class QuoteRequest(BaseModel):
    """Same fields as the portal's quote form. origin/destination: UN/LOCODE, IATA code, a city name or an address object."""
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "origin": "EETLL", "destination": "FITMP", "mode": "road", "incoterm": "FCA", "incoterm_place": "Tallinn",
        "ready_date": "2026-11-02", "cargo": {"load_type": "loose", "packages": 2, "package_type": "pallet_eur",
                                              "weight_kg": 600, "volume_m3": 2.0, "value_cents": 800000,
                                              "description": "Office furniture"},
        "insurance": False, "customs_clearance": True, "notes": "Tail lift needed at delivery"}})
    origin: dict | str
    destination: dict | str
    mode: str | None = Field(None, description="road | sea | air | rail; omit to get every mode that fits")
    ready_date: str | None = Field(None, description="YYYY-MM-DD when the goods are ready")
    incoterm: str | None = Field(None, description="Incoterms 2020 code, e.g. FCA, DAP, FOB")
    incoterm_place: str | None = None
    customer_role: str | None = Field(None, description="shipper | consignee")
    cargo: dict
    insurance: dict | bool | None = None
    customs_clearance: bool | None = None
    notes: str | None = None


class Party(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    address: dict = Field(description="{street, city, postal_code, country (ISO 3166-1 alpha-2)}")
    contact: dict | None = Field(None, description="{name, phone, email}")
    eori: str | None = None
    vat_id: str | None = None


class Stop(BaseModel):
    model_config = ConfigDict(extra="allow")
    address: dict | None = None
    date: str | None = None
    window: str | None = Field(None, description="e.g. 08-16")
    contact: dict | None = None
    instructions: str | None = None


class BookRequest(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "option_id": "<option id from the quote>", "accept_terms": True, "customer_reference": "PO-4711",
        "shipper": {"name": "Tallinna Mööbel OÜ", "address": {"street": "Peterburi tee 46", "city": "Tallinn",
                                                             "postal_code": "11415", "country": "EE"}},
        "consignee": {"name": "Testi Oy", "address": {"street": "Hatanpään valtatie 24", "city": "Tampere",
                                                     "postal_code": "33100", "country": "FI"}},
        "pickup": {"date": "2026-11-03", "window": "08-16"}}})
    option_id: str | None = Field(None, description="Option to book; default = the recommended option")
    shipper: Party
    consignee: Party
    notify: Party | None = None
    pickup: Stop = Stop()
    delivery: Stop = Stop()
    cargo: dict | None = None
    incoterm_place: str | None = None
    customer_reference: str | None = None
    po_numbers: list[str] = []
    special_instructions: str | None = None
    accept_terms: bool = Field(False, description="Must be true: NSAB 2015 (PSYM 2015) and Huolitsija's general terms")


class WebhookIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "url": "https://erp.example.com/huolitsija/webhook", "events": ["shipment.status_changed", "invoice.created"],
        "description": "ERP production"}})
    url: str
    events: list[str] = Field(default_factory=lambda: ["*"], description="Event names or ['*'] for all")
    description: str | None = None


async def _api_audit(ctx: ApiContext, action: str, entity: str, entity_id: str | None, data: dict | None = None):
    await audit(ctx.user, action, entity, entity_id, {"api_key_id": ctx.key["id"], "api_key_prefix": ctx.key.get("prefix"),
                                                      **(data or {})}, company_id=ctx.company["id"])


# ------------------------------------------------------------------------------------------------ account
@router.get("/me", tags=["Account"], summary="The key and company you are authenticated as")
async def me(ctx: ApiContext = Depends(require_scope())):
    c = ctx.company
    return {"company": {k: c.get(k) for k in ("id", "name", "business_id", "country", "kyc_status", "sanctions_status")},
            "key": {k: ctx.key.get(k) for k in ("id", "name", "prefix", "scopes", "environment", "rate_limit_per_min", "created_at")},
            "scopes_available": list(SCOPES)}


# ------------------------------------------------------------------------------------------------ quotes
@router.post("/quotes", tags=["Quotes"], summary="Request a quote",
             description="Prices the request with the same engine as the portal. The quote comes back `sent` (bookable) "
                         "or `pending_approval` (a person or the Autopilot reviews it first; you get `quote.created`).")
async def create_quote(body: QuoteRequest, ctx: ApiContext = Depends(require_scope("quotes:write"))):
    data = body.model_dump(exclude_none=True)
    q = await quotes_routes.create_quote(data, user=ctx.user, company=ctx.company, source="api", send=True)
    await _api_audit(ctx, "api_quote_created", "quote", q["id"], {"number": q.get("number")})
    return {"quote": quotes_routes.customer_view(q)}


@router.get("/quotes", tags=["Quotes"], summary="List your quotes")
async def list_quotes(status: str | None = Query(None, description="draft|pending_approval|sent|accepted|booked|expired|rejected"),
                      limit: int = Query(100, ge=1, le=300), ctx: ApiContext = Depends(require_scope("quotes:read"))):
    out = await quotes_routes.list_quotes(status=status, source=None, q=None, company_id=None, include_anonymous=False,
                                          limit=limit, user=ctx.user)
    return {"items": out["items"], "total": out["total"]}


@router.get("/quotes/{quote_id}", tags=["Quotes"], summary="Get a quote with its options and booking blockers")
async def get_quote(quote_id: str, ctx: ApiContext = Depends(require_scope("quotes:read"))):
    return await quotes_routes.get_quote(quote_id, user=ctx.user)


@router.post("/quotes/{quote_id}/book", tags=["Bookings"], summary="Book a quote option",
             description="Creates the shipment. Needs a verified company (KYC approved, sanctions clear); otherwise 409 "
                         "explains what is missing. One booking per quote.")
async def book_quote(quote_id: str, body: BookRequest, request: Request,
                     ctx: ApiContext = Depends(require_scope("bookings:write"))):
    data = body.model_dump(exclude_none=True)
    if not data.get("option_id"):
        q = await quotes_routes.load_quote(quote_id, ctx.user)
        rec = quotes_routes.recommended_option(q)
        if not rec:
            raise HTTPException(409, "This quote has no bookable option.")
        data["option_id"] = rec["id"]
    out = await quotes_routes._create_booking(quotes_routes.BookingIn(quote_id=quote_id, **data), request, ctx.user)
    await _api_audit(ctx, "api_quote_booked", "quote", quote_id, {"shipment_id": out.get("shipment_id"),
                                                                  "option_id": data["option_id"]})
    return out


# ------------------------------------------------------------------------------------------------ shipments
@router.get("/shipments", tags=["Shipments"], summary="List your shipments")
async def list_shipments(status: str | None = Query(None, description="active or a status: booked, picked_up, in_transit, "
                                                                      "at_hub, customs_hold, out_for_delivery, delivered, exception"),
                         q: str | None = Query(None, description="Search reference, PO number, place, container, vessel"),
                         limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                         ctx: ApiContext = Depends(require_scope("shipments:read"))):
    out = await shipments_routes.list_shipments(status=status, mode=None, q=q, company_id=None, needs_action_only=False,
                                                include_cancelled=False, limit=limit, offset=offset, user=ctx.user)
    return {"items": out["items"], "total": out["total"]}


async def _resolve_shipment(id_or_ref: str, ctx: ApiContext) -> dict:
    # id and our reference are unique; a customer reference can repeat, then the newest shipment wins
    s = await db.shipments.find_one({"company_id": ctx.company["id"],
                                     "$or": [{"id": id_or_ref}, {"reference": id_or_ref},
                                             {"references.customer_ref": id_or_ref}]}, {"_id": 0, "id": 1},
                                    sort=[("created_at", -1)])
    if not s:
        raise HTTPException(404, "Shipment not found")
    return s


@router.get("/shipments/{id_or_ref}", tags=["Shipments"], summary="Get a shipment by id, Huolitsija reference or your reference")
async def get_shipment(id_or_ref: str, ctx: ApiContext = Depends(require_scope("shipments:read"))):
    s = await _resolve_shipment(id_or_ref, ctx)
    return await shipments_routes.get_shipment(s["id"], user=ctx.user)


@router.get("/shipments/{id_or_ref}/events", tags=["Tracking"], summary="Tracking events of a shipment",
            description="`format=dcsa` returns DCSA Track & Trace 2.2 events.")
async def shipment_events(id_or_ref: str, format: str = Query("huolitsija", pattern="^(huolitsija|dcsa)$"),
                          ctx: ApiContext = Depends(require_scope("tracking:read"))):
    s = await _resolve_shipment(id_or_ref, ctx)
    out = await shipments_routes.shipment_events(s["id"], user=ctx.user)
    if format == "dcsa":
        try:
            from services.edi import dcsa
        except ImportError:
            raise HTTPException(501, "DCSA output is not deployed on this server yet.") from None
        shipment = await db.shipments.find_one({"id": s["id"]}, NO_ID)
        return {"events": dcsa.tnt_events(shipment, out["items"])}
    return out


# ------------------------------------------------------------------------------------------------ documents
@router.post("/documents", tags=["Documents"], summary="Upload a document (PDF, image, XLSX…) for a shipment",
             description="Multipart form: `file`, optional `type` (commercial_invoice, packing_list, …) and `shipment_id`. "
                         "The document is read by the same extraction as portal uploads.")
async def upload_document(request: Request, file: UploadFile = File(...), type: str | None = Form(None),
                          shipment_id: str | None = Form(None), ctx: ApiContext = Depends(require_scope("documents:write"))):
    if shipment_id:
        shipment_id = (await _resolve_shipment(shipment_id, ctx))["id"]
    doc = await documents_routes.upload_document(request, file=file, type=type, shipment_id=shipment_id, company_id=None,
                                                 user=ctx.user)
    await _api_audit(ctx, "api_document_uploaded", "document", (doc or {}).get("id"), {"shipment_id": shipment_id})
    return doc


# ------------------------------------------------------------------------------------------------ invoices
@router.get("/invoices", tags=["Invoices"], summary="List your invoices")
async def list_invoices(request: Request, status: str | None = Query(None, description="open, overdue, paid, … (comma list)"),
                        shipment_id: str | None = None, limit: int = Query(200, ge=1, le=1000),
                        ctx: ApiContext = Depends(require_scope("invoices:read"))):
    out = await invoices_routes.list_invoices(request, status=status, company_id=None, shipment_id=shipment_id, kind=None, q=None,
                                              limit=limit, user=ctx.user)
    return {"items": out.get("items", []), "total": out.get("total", len(out.get("items", [])))}


@router.get("/invoices/{invoice_id}", tags=["Invoices"], summary="Get an invoice with lines and payment options")
async def get_invoice(invoice_id: str, request: Request, ctx: ApiContext = Depends(require_scope("invoices:read"))):
    return await invoices_routes.get_invoice(invoice_id, request, user=ctx.user)


# ------------------------------------------------------------------------------------------------ webhooks
@router.get("/webhooks", tags=["Webhooks"], summary="List webhook endpoints")
async def list_webhooks(ctx: ApiContext = Depends(require_scope("webhooks:manage"))):
    return {"items": await webhooks_out.list_endpoints(ctx.company["id"]), "events": list(webhooks_out.EVENTS)}


@router.post("/webhooks", tags=["Webhooks"], summary="Register a webhook endpoint",
             description="Returns the signing `secret` once. Store it; it is never shown again.")
async def create_webhook(body: WebhookIn, ctx: ApiContext = Depends(require_scope("webhooks:manage"))):
    ep, secret = await webhooks_out.create_endpoint(ctx.company["id"], url=body.url, events=body.events,
                                                    description=body.description, created_by=ctx.user)
    await _api_audit(ctx, "api_webhook_created", "webhook_endpoint", ep["id"], {"url": ep["url"], "events": ep["events"]})
    return {"endpoint": ep, "secret": secret}


@router.delete("/webhooks/{endpoint_id}", tags=["Webhooks"], summary="Delete a webhook endpoint")
async def delete_webhook(endpoint_id: str, ctx: ApiContext = Depends(require_scope("webhooks:manage"))):
    ep = await webhooks_out.delete_endpoint(ctx.company["id"], endpoint_id)
    await _api_audit(ctx, "api_webhook_deleted", "webhook_endpoint", endpoint_id, {"url": ep["url"]})
    return {"endpoint": ep}


@router.post("/webhooks/{endpoint_id}/test", tags=["Webhooks"], summary="Send a signed `ping` to the endpoint now")
async def test_webhook(endpoint_id: str, ctx: ApiContext = Depends(require_scope("webhooks:manage"))):
    await webhooks_out.get_endpoint(ctx.company["id"], endpoint_id)
    rows = await webhooks_out.enqueue(ctx.company["id"], "ping", {"message": "Huolitsija webhook test"}, only_endpoint=endpoint_id)
    return {"delivery": await webhooks_out.deliver_now(rows[0]["id"])}


@router.get("/webhooks/{endpoint_id}/deliveries", tags=["Webhooks"], summary="Recent deliveries to an endpoint")
async def webhook_deliveries(endpoint_id: str, limit: int = Query(50, ge=1, le=200),
                             ctx: ApiContext = Depends(require_scope("webhooks:manage"))):
    await webhooks_out.get_endpoint(ctx.company["id"], endpoint_id)
    return {"items": await webhooks_out.list_deliveries(ctx.company["id"], endpoint_id, limit)}


# ------------------------------------------------------------------------------------------------ docs
_schema_cache: dict | None = None


def openapi_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        s = get_openapi(title="Huolitsija Shipper API", version="1.0.0", description=API_DESCRIPTION, routes=router.routes,
                        tags=[{"name": n} for n in ("Account", "Quotes", "Bookings", "Shipments", "Tracking", "Documents",
                                                    "Invoices", "Webhooks")])
        s["servers"] = [{"url": "https://huolitsija-app.emergent.host", "description": "Production"}]
        s.setdefault("components", {})["securitySchemes"] = {
            "ApiKeyBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "huo_live_…",
                             "description": "API key from the customer portal (Rajapinta / API)"},
            "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        }
        s["security"] = [{"ApiKeyBearer": []}, {"ApiKeyHeader": []}]
        s["x-webhook-events"] = {k: v[1] for k, v in webhooks_out.EVENTS.items()}
        _schema_cache = s
    return _schema_cache


@docs_router.get("/openapi.json")
async def openapi_json():
    return JSONResponse(openapi_schema())


DOCS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Huolitsija Shipper API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{margin:0}</style></head>
<body><redoc spec-url="/api/v1/openapi.json" hide-hostname="false" expand-responses="200"></redoc>
<script src="https://cdn.redoc.ly/redoc/v2.1.5/bundles/redoc.standalone.js"></script></body></html>"""


@docs_router.get("/docs", response_class=HTMLResponse)
async def docs_page():
    return HTMLResponse(DOCS_HTML, headers={"Cache-Control": "public, max-age=300"})


routers = [docs_router, router]
