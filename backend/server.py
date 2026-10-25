"""Huolitsija API (FastAPI + MongoDB). Every route is under /api (Emergent ingress rule)."""
import importlib
import logging

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from core import config
from core.db import client, db, ensure_indexes
from core.lifecycle import run_startup, start_loops, stop_loops
from core.security import load_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

app = FastAPI(title="Huolitsija API", version="1.0.0")

# Route modules. Each exposes `router` (and optionally `routers`, a list).
ROUTE_MODULES = [
    "routes.auth", "routes.team", "routes.admin", "routes.portal", "routes.partner_network", "routes.procurement",
    # shipments, tracking, map, partner portal
    "routes.locations", "routes.shipments", "routes.map", "routes.partner", "routes.visibility",
    # quotes, booking, carriers, rates, public instant quote
    "routes.public_quote", "routes.public_surcharges", "routes.quotes", "routes.carriers", "routes.legal", "routes.global_quote", "routes.runbook",
    # documents, customs, company profile / KYC, ops customers
    "routes.customs_export", "routes.documents", "routes.customs_reference", "routes.customs_controls_ref", "routes.customs_bti",
    "routes.customs_readiness",
    "routes.customs_offices", "routes.customs", "routes.companies", "routes.customs_transit", "routes.customs_ens",
    "routes.trade_docs",
    # invoices, payments, messages, exceptions, claims
    "routes.invoices", "routes.messages", "routes.exceptions", "routes.claims",
    # finance (s13): accruals, carrier invoices, payables, FX, collections, P&L, VAT, ledger
    "routes.finance", "routes.finance_ap", "routes.finance_pay", "routes.finance_reports", "routes.test_tenant_finance",
    # e-mail channel: customers who only e-mail (s6)
    "routes.email_channel",
    # execution after the carrier booking: SI/VGM, cut-offs, slots, consolidation, cross-dock, warehouse (s9)
    "routes.execution",
    # compliance (s12): party screening, dangerous goods, owners/PEP, GDPR + retention
    "routes.compliance_screening", "routes.compliance_dg", "routes.compliance_owners", "routes.privacy",
    # s14: notification channels (SMS, e-mail delivery status)
    "routes.channels",
    # management insight: KPIs and forecast (s14)
    "routes.insights",
    # shipper REST API v1 (API keys, webhooks, docs) and its portal management (s14)
    "routes.api_v1", "routes.api_keys",
    # EDI gateway: EDIFACT IFTMIN/IFTMCS/IFTSTA, DCSA JSON, SFTP + AS2 (s14)
    "routes.edi",
    # Autopilot operations: durable jobs, agents, owner queue, monitoring, webhooks and feeds
    "routes.autopilot",
    "routes.payments_ops",
    "routes.webhooks_feeds",
    # s7 commercial: CRM/prospecting, tenders, margin rules, contract tariffs, credit, cargo insurance
    "routes.commercial",
    # customs AEO-style controls (s11): settings, per-declaration status, audit CSV, KPIs, retention job
    "routes.customs_controls",
    "routes.customs_origin",  # s11: origin and tariff preference (DE 4/17), REX, PSR, quota status
    # autonomous quote demo (off unless DEMO_AUTOQUOTE_ENABLED=true)
    "routes.demo_autoquote",
    "routes.demo_public",
    "routes.demo_world",
    # demo data (runs last)
    "routes.seed",
]

for mod_name in ROUTE_MODULES:
    mod = importlib.import_module(mod_name)
    for r in getattr(mod, "routers", None) or [getattr(mod, "router")]:
        app.include_router(r)

# s11 customs AEO controls answer refusals as {detail, code, fi, en, reasons} (403 customs_not_authorised, 409 ...).
from services.customs_aeo import ControlRefusal, control_refusal_handler  # noqa: E402
import services.test_tenant_bootstrap  # noqa: E402,F401 - registers its startup hook
import services.demo_world.boundary  # noqa: E402,F401 - demo world sandbox boundary (DEMO_WORLD_SANDBOX) and its clock
import services.demo_world.engine  # noqa: E402,F401 - outside actors react to demo.offer_sent and captured mail
import services.demo_world.observer  # noqa: E402,F401 - map events project actual domain outcomes
import core.bounded  # noqa: E402,F401 - registers the request beat and the orphaned-request sweep

app.add_exception_handler(ControlRefusal, control_refusal_handler)


class GZipExceptStreams:
    """gzip JSON (the map snapshot is ~11 MB raw for 2,000 shipments) but never SSE: Starlette < 0.40 would buffer it."""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].endswith("/stream"):
            accept = dict(scope.get("headers") or []).get(b"accept", b"")
            if b"text/event-stream" not in accept:
                return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


class SecurityHeaders:
    """HSTS, nosniff, referrer and framing policy on every API response. Pure ASGI so SSE is never buffered.

    HSTS here covers the whole host. The HTML is served by Emergent's static host, so index.html adds its own
    frame guard and referrer policy."""

    HEADERS = [
        (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"content-security-policy", b"frame-ancestors 'self' https://*.emergent.sh https://*.emergentagent.com"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers") or []}
                message["headers"] = list(message.get("headers") or []) + [h for h in self.HEADERS if h[0] not in present]
            await send(message)

        return await self.app(scope, receive, send_with_headers)


app.add_middleware(GZipExceptStreams)
app.add_middleware(SecurityHeaders)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=config.CORS_ORIGINS if config.CORS_ORIGINS != ["*"] else [],
    allow_origin_regex=".*" if config.CORS_ORIGINS == ["*"] else None,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await load_secret(db)
    await ensure_indexes()
    await run_startup()
    from services import gazetteer

    gazetteer._start_warm_up()  # builds the any-city index in a thread, off the event loop (every process)
    start_loops()
    log.info("Huolitsija API started (db=%s)", config.DB_NAME)


@app.on_event("shutdown")
async def shutdown():
    await stop_loops()
    client.close()
