"""Cargo insurer adapters (s7). One provider is active, chosen by env INSURANCE_PROVIDER (else setting insurance_provider).

    redkik      Redkik Oy cargo insurance platform, REST API v2 (OAuth 2.0 client credentials).
                Public spec: https://sales.app.redkik.com/api/v2/apidoc/ (QuoteService, UserService),
                usage guide https://redkik.com/wp-content/uploads/redkik-3-api-usage.pdf.
                Env: REDKIK_BASE_URL (default production https://sales.app.redkik.com; Redkik's test host is sandbox.app.redkik.com),
                REDKIK_CLIENT_ID, REDKIK_CLIENT_SECRET (an API token set Redkik creates for our organisation once the insurer
                contract is signed),
                REDKIK_POLICY_ID (optional when one policy is assigned), REDKIK_COVER_TYPES (optional JSON {"ICC_A": "<coverType>"}),
                REDKIK_COMMODITY_ID (optional fallback commodity UUID).

There is no stand-in insurer. Without the Redkik API token set the state is explicit: code `insurer_not_configured`
(HTTP 503), state `live_ready`, blocked on the insurer contract (BLOCKED_ON). Nothing is quoted or bound in that state.

Every provider returns plain dicts (normalised below); services/insurance.py stores them.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("insurer")

UA = {"User-Agent": "Huolitsija Insurance/1.0 (+https://huolitsija.fi)", "Accept": "application/json"}
BLOCKED_ON = "Insurer contract with Redkik: Redkik issues the API token set (REDKIK_CLIENT_ID, REDKIK_CLIENT_SECRET) to our organisation"
BLOCKED_ON_FI = "Vakuutussopimus Redkikin kanssa: Redkik antaa organisaatiollemme API-tunnukset (REDKIK_CLIENT_ID, REDKIK_CLIENT_SECRET)"
NOT_CONFIGURED = {"state": "live_ready", "blocked_on": BLOCKED_ON, "blocked_on_fi": BLOCKED_ON_FI}
COVERS = ("ICC_A", "ICC_B", "ICC_C")
REDKIK_SANDBOX = "https://sandbox.app.redkik.com"
REDKIK_PRODUCTION = "https://sales.app.redkik.com"


class InsurerError(Exception):
    """Provider refused or failed. `status` is the HTTP status we pass on; `code` a stable machine code."""

    def __init__(self, message: str, *, code: str = "insurer_error", status: int = 502, detail: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def trim(obj, max_str: int = 2000, max_list: int = 25, depth: int = 0):
    """Keep provider responses readable in Mongo: long strings (clause wordings) and long lists are cut."""
    if depth > 8:
        return "…"
    if isinstance(obj, dict):
        return {k: trim(v, max_str, max_list, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [trim(v, max_str, max_list, depth + 1) for v in obj[:max_list]]
        if len(obj) > max_list:
            out.append(f"… {len(obj) - max_list} more")
        return out
    if isinstance(obj, str) and len(obj) > max_str:
        return obj[:max_str] + "…"
    return obj


def redact(headers: dict) -> dict:
    return {k: ("<redacted>" if k.lower() in ("authorization", "x-api-key") else v) for k, v in headers.items()}


# ----------------------------------------------------------------------------------------------- Redkik

_CONVEYANCE = {"sea": "SEA", "air": "AIR", "road": "ROAD", "rail": "RAIL"}


class Redkik:
    name = "redkik"
    label = "Redkik (cargo insurance platform)"
    stub = False
    supports_cancel = True
    dangerous_goods = "refer"   # Redkik policies decide per commodity; we never auto-bind DG (legal point)

    def __init__(self):
        self.base = _env("REDKIK_BASE_URL", REDKIK_PRODUCTION).rstrip("/")
        self.client_id = _env("REDKIK_CLIENT_ID")
        self.client_secret = _env("REDKIK_CLIENT_SECRET")
        self.policy_id = _env("REDKIK_POLICY_ID") or None
        self.fallback_commodity = _env("REDKIK_COMMODITY_ID") or None
        try:
            self.cover_types = json.loads(_env("REDKIK_COVER_TYPES") or "{}")
        except ValueError:
            self.cover_types = {}
        self._token: tuple[str, float] | None = None

    @property
    def mode(self) -> str:
        return "sandbox" if "sandbox" in self.base or "staging" in self.base else "live"

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def info(self) -> dict:
        return {"provider": self.name, "label": self.label, "mode": self.mode, "base_url": self.base,
                "configured": self.configured(), "stub": False, "supports_cancel": True,
                "state": "live" if self.configured() else "live_ready",
                "blocked_on": None if self.configured() else BLOCKED_ON, "blocked_on_fi": None if self.configured() else BLOCKED_ON_FI,
                "missing": [] if self.configured() else ["REDKIK_CLIENT_ID", "REDKIK_CLIENT_SECRET"],
                "spec": "https://sales.app.redkik.com/api/v2/apidoc/?urls.primaryName=QuoteService"}

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, timeout=45, headers=UA)

    async def status(self) -> dict:
        """Public health endpoint of the quote service (no credentials)."""
        async with await self._client() as c:
            r = await c.get("/api/v2/quote/status")
        return {"http": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]}

    async def token(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        if not self.configured():
            raise not_configured()
        async with await self._client() as c:
            r = await c.post("/api/v2/user/oauth/token", json={"grant_type": "client_credentials",
                                                                "client_id": self.client_id, "client_secret": self.client_secret})
        body = _json(r)
        if r.status_code != 200 or not body.get("access_token"):
            raise InsurerError(f"Redkik refused the API credentials: {body.get('error_description') or body.get('error') or r.status_code}",
                               code="insurer_auth_failed", status=502, detail={"http": r.status_code, "body": trim(body)})
        self._token = (body["access_token"], time.time() + float(body.get("expires_in") or 300))
        return self._token[0]

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self.token()}", **kw.pop("headers", {})}
        async with await self._client() as c:
            r = await c.request(method, path, headers=headers, **kw)
        if r.status_code == 401:  # token revoked early: once more with a fresh one
            self._token = None
            headers["Authorization"] = f"Bearer {await self.token()}"
            async with await self._client() as c:
                r = await c.request(method, path, headers=headers, **kw)
        return r

    async def search_commodities(self, query: str, take: int = 10) -> list[dict]:
        """GET /api/v2/quote/commodities/search — public in the spec (no token)."""
        async with await self._client() as c:
            r = await c.get("/api/v2/quote/commodities/search", params={"query": query, "take": take})
        if r.status_code != 200:
            raise InsurerError(f"Redkik commodity search failed ({r.status_code})", detail={"body": r.text[:300]})
        return r.json()

    async def commodity_for(self, cargo: dict) -> dict:
        """Redkik commodity for our cargo: HS code first (Redkik names HS rows "HS/HTS 940161: …"), then the description."""
        hs = re.sub(r"\D", "", str(cargo.get("hs_code") or cargo.get("hs_hint") or ""))
        tried = []
        desc_words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", cargo.get("description") or "")}

        def overlap(row):
            name = (row.get("name") or "").lower()
            return sum(1 for w in desc_words if w.rstrip("s") in name)

        for q in dict.fromkeys([hs[:6], hs[:4]] if len(hs) >= 4 else []):
            rows = await self.search_commodities(q, 25)
            tried.append(q)
            hs_rows = [x for x in rows if (x.get("name") or "").startswith(f"HS/HTS {q}")]
            if hs_rows:
                best = max(hs_rows, key=lambda x: (overlap(x), -hs_rows.index(x)))
                return {"id": best["id"], "name": best["name"], "matched_on": f"hs:{q}"}
        words = [w for w in re.findall(r"[A-Za-z]{4,}", cargo.get("description") or "")][:3]
        for w in words:
            rows = await self.search_commodities(w, 5)
            tried.append(w)
            if rows:
                return {"id": rows[0]["id"], "name": rows[0]["name"], "matched_on": f"text:{w}"}
        if self.fallback_commodity:
            return {"id": self.fallback_commodity, "name": None, "matched_on": "REDKIK_COMMODITY_ID"}
        raise InsurerError("No Redkik commodity matches this cargo; add the HS code.", code="commodity_unknown", status=422,
                           detail={"tried": tried})

    def quote_body(self, req: dict, commodity: dict) -> dict:
        """QuoteRequest (QuoteService spec). Money: insuredValue in currency units; Redkik answers in cents."""
        start, end = req["start"], req["end"]
        body = {
            "commodities": [{"commodityId": commodity["id"], "insuredValue": round(req["insured_value_cents"] / 100, 2),
                             "currencyId": req["currency"]}],
            "commodityDescription": req["description"],
            "startDate": start, "endDate": end,
            "conveyanceType": _CONVEYANCE.get(req.get("mode") or "", "ALL"),
            "trackingCode": req.get("reference") or None,
            "metadata": {"huolitsija_subject": req["subject"], "requested_cover": req["cover"]},
        }
        for end_name, loc in (("origin", req["origin"]), ("destination", req["destination"])):
            if loc.get("unlocode"):
                body[end_name] = {"formatted": loc["unlocode"], "country": loc.get("country")}
                body[end_name + "Type"] = "LOCODE"
            else:
                body[end_name] = {"formatted": loc.get("label") or loc.get("name") or loc.get("country"),
                                  "city": loc.get("city"), "country": loc.get("country")}
                body[end_name + "Type"] = "CITY"
        if req.get("loading_port"):
            body.update(loadingSpace=req["loading_port"], loadingSpaceType="PORT_LOCODE")
        if req.get("discharge_port"):
            body.update(dischargeSpace=req["discharge_port"], dischargeSpaceType="PORT_LOCODE")
        if req.get("vessel_imo"):
            body.update(conveyanceSpace=str(req["vessel_imo"]), conveyanceSpaceType="VESSEL_IMO")
        elif req.get("vessel_name"):
            body.update(unableToFindVessel=True, customVesselName=req["vessel_name"])
        if req.get("voyage"):
            body["voyageNumber"] = req["voyage"]
        if req.get("carrier"):
            body["courier"] = req["carrier"]
        cust = req.get("customer") or {}
        if cust.get("name"):
            body["customer"] = {"type": "BUSINESS", "businessName": cust["name"], "taxId": cust.get("business_id"),
                                "reference": cust.get("id"),
                                "address": {"formatted": cust.get("address") or cust.get("country") or "FI",
                                            "country": cust.get("country") or "FI"}}
        if self.policy_id:
            body["policyId"] = self.policy_id
        if self.cover_types.get(req["cover"]):
            body["coverType"] = self.cover_types[req["cover"]]
        return {k: v for k, v in body.items() if v is not None}

    async def quote(self, req: dict) -> dict:
        await self.token()  # credentials first: no outside calls for a quote we cannot get
        commodity = await self.commodity_for(req["cargo"])
        body = self.quote_body(req, commodity)
        r = await self._call("POST", "/api/v2/quote/quotes/quote", json=body)
        data = _json(r)
        if r.status_code != 200 or not isinstance(data, list):
            raise InsurerError(f"Redkik did not quote ({r.status_code}): {_err(data)}", code="insurer_quote_failed",
                               detail={"http": r.status_code, "body": trim(data)})
        offers = data
        if not offers:
            raise InsurerError("Redkik returned no offer for this shipment.", code="insurer_no_offer", status=422)
        offer = next((o for o in offers if o.get("accepted")), offers[0])
        return {
            "provider_quote_id": offer["id"], "accepted": bool(offer.get("accepted")),
            "amendments": offer.get("amendments") or [],
            "premium_cents": int(offer["totalCost"]), "insurer_premium_cents": int(offer.get("insurerPremium") or 0),
            "fees_cents": int(offer.get("technologyFee") or 0) + int(offer.get("bookingFee") or 0),
            "taxes_cents": sum(int(offer.get(k) or 0) for k in ("insurerPremiumTax", "technologyFeeTax", "bookingFeeTax")),
            "currency": req["currency"], "currency_symbol": offer.get("currencySymbol"),
            "deductibles": [{k: d.get(k) for k in ("name", "label", "type", "value", "formulaValue")} for d in offer.get("deductibles") or []],
            "clauses": [{"id": c.get("id"), "name": c.get("name"), "version": c.get("versionReference")} for c in offer.get("clauses") or []],
            "sanction_sources": offer.get("sanctionSources") or [],
            "valid_until": None,
            "commodity": commodity,
            "request": body, "response": trim(offers),
        }

    async def bind(self, provider_quote_id: str, metadata: dict | None = None) -> dict:
        files = {"offerId": (None, provider_quote_id)}
        if metadata:
            files["metadata"] = (None, json.dumps(metadata), "application/json")
        r = await self._call("POST", "/api/v2/quote/bookings/purchase", files=files)
        data = _json(r)
        if r.status_code != 200 or not isinstance(data, dict) or not data.get("id"):
            raise InsurerError(f"Redkik did not bind the offer ({r.status_code}): {_err(data)}", code="insurer_bind_failed",
                               detail={"http": r.status_code, "body": trim(data)})
        st = str(data.get("status") or "").upper()
        status = {"ACTIVE": "bound", "COMPLETE": "bound", "CANCELLED": "cancelled", "PAYMENT_FAILED": "failed"}.get(st, "pending")
        return {"provider_policy_id": data["id"], "certificate_number": data.get("reference"), "status": status,
                "provider_status": st, "payment_url": data.get("paymentUrl"), "response": trim(data)}

    async def certificate(self, provider_policy_id: str) -> tuple[bytes, str] | None:
        """GET /api/v2/quote/bookings/{id}/certificate (public in the spec) -> PDF."""
        async with await self._client() as c:
            r = await c.get(f"/api/v2/quote/bookings/{provider_policy_id}/certificate", headers={"Accept": "application/pdf"})
        if r.status_code == 200 and r.content[:5] == b"%PDF-":
            return r.content, f"redkik-certificate-{provider_policy_id}.pdf"
        log.warning("redkik certificate %s: %s %s", provider_policy_id, r.status_code, r.text[:200])
        return None

    async def cancel(self, provider_policy_id: str) -> dict:
        r = await self._call("PATCH", f"/api/v2/quote/bookings/{provider_policy_id}/cancel")
        data = _json(r)
        if r.status_code != 200 or data is False:
            raise InsurerError(f"Redkik did not cancel the booking ({r.status_code}): {_err(data)}", code="insurer_cancel_failed",
                               detail={"http": r.status_code, "body": trim(data)})
        return {"cancelled": True, "response": data}


# ----------------------------------------------------------------------------------------------- selection

PROVIDERS = {"redkik": Redkik}
DOCUMENTED = {
    "redkik": "Redkik REST API v2 — public OpenAPI, sandbox host sandbox.app.redkik.com; API token set issued by Redkik.",
    "loadsure": "Loadsure — sandbox portal sandbox.loadsure.net exists, API spec not public, access only via Loadsure; no adapter.",
}


def provider_name(setting: str | None = None) -> str:
    return (_env("INSURANCE_PROVIDER") or (setting or "") or "redkik").strip().lower()


def not_configured() -> InsurerError:
    return InsurerError("No insurer contract yet: set REDKIK_CLIENT_ID and REDKIK_CLIENT_SECRET (issued by Redkik).",
                        code="insurer_not_configured", status=503, detail=dict(NOT_CONFIGURED))


def get_provider(setting: str | None = None, **_ignored):
    """The active insurer. Redkik is the only adapter; it is returned even without credentials so exclusions still
    run, and its quote/bind raise `insurer_not_configured` until the token set exists."""
    name = provider_name(setting)
    if name == "redkik":
        return Redkik()
    raise InsurerError(f"Unknown insurance provider '{name}'. The only insurer adapter is redkik.", code="insurer_unknown", status=503)


def _json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return {"text": r.text[:500]}


def _err(data) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error_description") or data.get("error") or data.get("name") or data)[:300]
    return str(data)[:300]


def cover_dates(etd: str | None, eta: str | None, transit_days: float | None = None) -> tuple[str, str]:
    """Cover from departure (not in the past) to ETA + 7 days (no ETA: + transit + 7, else + 45 days)."""
    def p(s):
        if not s:
            return None
        try:
            d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            d = datetime.fromisoformat(str(s)[:10])
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = max(p(etd) or now, now + timedelta(minutes=5))
    end = p(eta)
    if not end or end <= start:
        end = start + timedelta(days=(transit_days or 38))
    end = end + timedelta(days=7)
    f = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(f), end.strftime(f)
