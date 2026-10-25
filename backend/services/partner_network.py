"""Sourcing Autopilot (Hankinta-autopilotti): discovery -> RFQ e-mail -> reply ingestion -> parsing -> bounded
negotiation -> rate card. Internal names keep "partner_network" (settings key, routes, collections).

One cycle (`run_cycle`: durable job sourcing.cycle every 5 minutes while the switch is on, or "Run now"):
1. read the partner mailbox (IMAP) and act on replies (opt-outs are honoured even when the switch is off);
2. for every approved lane refresh public candidates (services.partner_discovery, cached 7 days) and send RFQs to the
   best contactable ones, at most `partner_rfqs_per_lane` open RFQs per lane;
3. remind once after 3 days without reply, close after 10;
4. send what the daily limit held back.

Every partner e-mail passes one gate (`send_partner_mail`): switch on, not killed, recipient not opted out, company
domain (B2B), daily limit, sender identification and an opt-out link. Every message in and out is kept in
`partner_messages`. Negotiation never goes above the ceiling min(max price, planned sell x (1 - target margin)).
"""
import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import re
import socket
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from core import config
from core.audit import audit
from core.autonomy import action
from core.db import db, register_index
from core.events import emit
from core.email import render, send_email, smtp_settings
from core.lifecycle import background_loop
from core.jobs import enqueue, job, schedule
from core.settings import get_settings, update_settings
from core.util import NO_ID, new_id, now, now_iso
from services import partner_discovery as discovery
from services.partner_parser import parse_reply

log = logging.getLogger("partner_network")
register_index("partner_outreach", "id", unique=True)
register_index("partner_outreach", "campaign_key", unique=True)
register_index("partner_outreach", "ref", unique=True, sparse=True)
register_index("partner_replies", "message_id", unique=True)
register_index("partner_messages", "id", unique=True)
# partial, not sparse: direction is always present, so a sparse compound index would still collide on null ids
register_index("partner_messages", [("direction", 1), ("message_id", 1)], unique=True, name="partner_msg_dir_mid",
               partialFilterExpression={"message_id": {"$type": "string"}})
register_index("partner_messages", [("outreach_id", 1), ("at", 1)])
register_index("partner_optouts", "email", unique=True, sparse=True)
register_index("partner_optouts", "domain")
register_index("partner_send_counters", "day", unique=True)
register_index("rate_cards", "outreach_id", unique=True, sparse=True)
register_index("lane_rate_requests", "id", unique=True)
register_index("lane_rate_requests", [("lane", 1), ("status", 1)])

HEL = ZoneInfo("Europe/Helsinki")
SERVICE = {"road": "ROAD_FTL", "sea": "FCL", "air": "AIR", "rail": "RAIL_FCL"}
BASIS = {"road": "PER_SHIPMENT", "sea": "PER_CONTAINER", "air": "PER_KG", "rail": "PER_CONTAINER"}
SERVICE_TEXT = {
    "road": ("täysperävaunu (FTL, 13,6 lvm, enintään 24 t), lastaus- ja purkupaikka lähtö- ja määrämaassa",
             "full trailer (FTL, 13.6 ldm, up to 24 t), loading and unloading in the origin and destination country"),
    "sea": ("FCL 40' HC konttirahti satamasta satamaan", "FCL 40' HC container, port to port"),
    "air": ("lentorahti, hinta per rahdituskilo", "air freight, price per chargeable kg"),
    "rail": ("40' HC konttirahti rautateitse terminaalista terminaaliin", "40' HC container by rail, terminal to terminal"),
}
SERVICE_LABEL = {"road": "full truck (FTL)", "sea": "FCL 40' HC", "air": "air freight", "rail": "rail 40' HC"}
BASIS_TEXT = {"PER_SHIPMENT": ("per lähetys (yksi auto)", "per shipment (one truck)"),
              "PER_CONTAINER": ("per kontti", "per container"), "PER_KG": ("per kg", "per kg")}
# Explicit on-demand services; scheduled country-wide campaigns retain their original defaults.
REQUEST_SERVICES = {
    "ROAD_FTL": ("road", "PER_SHIPMENT", "täyskuorma", "full truck"),
    "ROAD_LTL": ("road", "PER_KG", "kappaletavara, rahdituspaino 333 kg/m³ tai 1 850 kg/lvm", "groupage, chargeable weight 333 kg/m³ or 1,850 kg/ldm"),
    "FCL": ("sea", "PER_CONTAINER", "täysi kontti", "full container"),
    "LCL": ("sea", "PER_WM", "merikappaletavara, W/M: 1 m³ tai 1 000 kg", "sea groupage, W/M: 1 m³ or 1,000 kg"),
    "AIR": ("air", "PER_KG", "lentorahti, tilavuuspaino 1:6000", "air freight, volumetric weight 1:6000"),
    "COURIER": ("air", "PER_KG", "pikakuriiri, tilavuuspaino 1:5000", "express courier, volumetric weight 1:5000"),
    "RAIL_FCL": ("rail", "PER_CONTAINER", "rautatiekontti", "rail container"),
}
BASIS_TEXT["PER_WM"] = ("per W/M (vähintään 1)", "per W/M (minimum 1)")

COUNTRY_FI = {"FI": "Suomi", "SE": "Ruotsi", "EE": "Viro", "DE": "Saksa", "PL": "Puola", "LT": "Liettua", "LV": "Latvia",
              "NO": "Norja", "DK": "Tanska", "NL": "Alankomaat", "CN": "Kiina", "US": "Yhdysvallat"}
REMIND_AFTER_DAYS, EXPIRE_AFTER_DAYS, RFQ_SHARE_OF_LIMIT = 3, 10, 0.8
STALE_WORK_MINUTES = 30
REF_RX = re.compile(r"\bRFQ-[A-Z0-9]{6}\b")


# ---------------------------------------------------------------- policy

def lane_key(mode, origin, destination):
    return f"{mode}:{origin}>{destination}"


def cost_limit(settings: dict, lane: str, sell_cents: int) -> int:
    if lane not in settings["partner_approved_lanes"]:
        raise HTTPException(409, "Lane is outside the approved partner policy")
    if type(sell_cents) is not int or sell_cents <= 0:
        raise HTTPException(400, "Enter the planned selling price in whole cents")
    margin_limit = int((Decimal(sell_cents) * (100 - Decimal(str(settings["partner_target_margin_pct"]))) / 100)
                       .to_integral_value(rounding=ROUND_FLOOR))
    return min(settings["partner_max_price_cents"], margin_limit)


def row_limit(settings: dict, row: dict) -> int:
    """Ceiling for one RFQ. On-demand lane requests (request_lane_rates) carry their own ceiling, never above the
    global maximum purchase price; approved lanes use the margin policy."""
    if row.get("limit_override_cents"):
        return min(int(row["limit_override_cents"]), settings["partner_max_price_cents"])
    return cost_limit(settings, row["lane"], row["sell_cents"])


def counter_amount(limit: int, round_no: int, max_rounds: int, anchor_pct: float) -> int:
    """Concession ladder: round 1 starts anchor_pct below the ceiling, the last round offers the ceiling itself.
    Whole euros, never above the ceiling."""
    if max_rounds <= 1:
        return limit // 100 * 100
    share = Decimal(str(anchor_pct)) * (max_rounds - round_no) / (max_rounds - 1) / 100
    value = int((Decimal(limit) * (1 - share)).to_integral_value(rounding=ROUND_FLOOR))
    return min(limit, value // 100 * 100)


def kill_env() -> bool:
    return config.env("PARTNER_NETWORK_KILL", "").lower() in ("1", "true", "yes", "on")


async def is_on(*, outreach: dict | None = None) -> bool:
    if kill_env():
        return False
    raw = await db.settings.find_one({"key": "app"}, {"_id": 0, "partner_network_enabled": 1, "autopilot_halted": 1}) or {}
    from services.demo_world import boundary
    scoped = await boundary.partner_autopilot(outreach)
    if scoped is not None:
        return bool(scoped) and not raw.get("autopilot_halted")
    return bool(raw.get("partner_network_enabled")) and not raw.get("autopilot_halted")


def helsinki_day(at: datetime | None = None) -> str:
    return (at or now()).astimezone(HEL).date().isoformat()


# ---------------------------------------------------------------- published contact (public website, SSRF-safe)

CONTACT_PATHS = ("", "/contact", "/contact-us", "/en/contact", "/yhteystiedot", "/fi/yhteystiedot", "/kontakt", "/ota-yhteytta")
MAX_HOPS = 3
_NOT_SALES = re.compile(r"privacy|abuse|no-?reply|press|media|career|jobs|rekry|gdpr|dpo|tietosuoja|invoice|lasku|"
                        r"webmaster|hostmaster|postmaster|@(example\.(com|org|net)|sentry|[\w.-]*wixpress\.com)|"
                        r"^(example|name|email|user)@", re.I)
_SALES = re.compile(r"sales|myynti|quote|tarjou|freight|rahti|cargo|logistic|customer|asiakas|booking|tilau|info", re.I)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10, follow_redirects=False)


async def _public_host(host: str) -> bool:
    addresses = await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, host, 443)
    return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)


async def _fetch_public(client: httpx.AsyncClient, url: str) -> tuple[str | None, str, str]:
    """GET one page, following at most MAX_HOPS redirects by hand: every hop must stay on HTTPS and resolve only to
    public addresses, so a redirect cannot reach internal services. Returns (text or None, final url, reason)."""
    for _ in range(MAX_HOPS + 1):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None, url, "redirect left HTTPS"
        if not await _public_host(parsed.hostname):
            return None, url, "does not resolve to a public address"
        async with client.stream("GET", url, headers={"User-Agent": discovery.USER_AGENT}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                return None, url, f"HTTP {r.status_code}"
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > 500_000:
                    break
                chunks.append(chunk)
            return html.unescape(b"".join(chunks).decode("utf-8", errors="replace")), url, "ok"
    return None, url, "too many redirects"


def _base_domain(host: str) -> str:
    return discovery.base_domain(host)


async def published_contact(carrier: dict) -> tuple[str | None, str]:
    """Read public evidence: the company's own site and its usual contact pages, same domain only."""
    website = carrier.get("website") or ""
    host = urlparse(website).hostname
    if not host or urlparse(website).scheme != "https":
        return None, "No public HTTPS website configured"
    domain, configured, checked = _base_domain(host), (carrier.get("contact_email") or "").lower(), []
    async with _client() as client:
        for path in CONTACT_PATHS:
            try:
                text, final, reason = await _fetch_public(client, urljoin(website, path) if path else website)
            except Exception as exc:
                text, final, reason = None, website, f"failed: {type(exc).__name__}"
            checked.append(f"{path or '/'} {reason}")
            if text is None:
                continue
            found = {e.lower() for e in re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", text, re.I)}
            if configured and configured in found:
                return carrier["contact_email"], final
            own = sorted(e for e in found if _base_domain(e.split("@")[1]) == domain and not _NOT_SALES.search(e)
                         and not re.search(r"\.(png|jpe?g|gif|svg|webp)$", e))
            if own:
                return sorted(own, key=lambda e: (not _SALES.search(e.split("@")[0]), e))[0], final
    return None, f"No published commercial email found on {website} (checked: {'; '.join(checked)})"


# ---------------------------------------------------------------- opt-out

def _optout_secret() -> bytes:
    return (config.env("PARTNER_OPTOUT_SECRET") or config.JWT_SECRET or "huolitsija-optout").encode()


def optout_token(email: str) -> str:
    raw = email.lower().strip().encode()
    sig = hmac.new(_optout_secret(), raw, hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + sig


def optout_email_from_token(token: str) -> str | None:
    try:
        b64, sig = token.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except (ValueError, TypeError):
        return None
    good = hmac.new(_optout_secret(), raw, hashlib.sha256).hexdigest()[:32]
    return raw.decode() if hmac.compare_digest(good, sig) else None


def optout_url(email: str) -> str:
    base = config.APP_BASE_URL or "http://localhost:8000"
    return f"{base}/api/partner-network/opt-out?t={quote(optout_token(email))}"


async def is_opted_out(email: str | None, domain: str | None = None) -> bool:
    email = (email or "").lower()
    q = [{"email": email}] if email else []
    dom = domain or (discovery.base_domain(email.split("@")[1]) if "@" in email else None)
    if dom:
        q.append({"domain": dom, "scope": "domain"})
    return bool(q) and bool(await db.partner_optouts.find_one({"$or": q}))


async def add_optout(email: str, *, source: str, scope: str = "address", note: str | None = None, actor: dict | None = None) -> dict:
    """Record an opt-out and stop everything open for that address (or the whole company domain)."""
    email = email.lower().strip()
    domain = discovery.base_domain(email.split("@")[1]) if "@" in email else None
    doc = {"email": email, "domain": domain, "scope": scope, "source": source, "note": note, "at": now_iso()}
    await db.partner_optouts.update_one({"email": email}, {"$setOnInsert": {"id": new_id(), **doc}}, upsert=True)
    match = {"$or": [{"contact_email": email}, {"send_to": email}] + ([{"domain": domain}] if scope == "domain" else [])}
    rows = await db.partner_outreach.find({**match, "status": {"$nin": ["accepted", "declined", "opted_out"]}}, NO_ID).to_list(500)
    for row in rows:
        await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "opted_out", "pending": None,
            "updated_at": now_iso()}, "$push": {"log": {"kind": "opted_out", "at": now_iso(), "source": source}}})
    cand_q = {"$or": [{"contact_email": email}] + ([{"domain": domain}] if scope == "domain" else [])}
    await db.partner_candidates.update_many(cand_q, {"$set": {"status": "opted_out", "updated_at": now_iso()}})
    await audit(actor or {"id": "sourcing", "name": "Sourcing Autopilot"}, "partner_opt_out", "partner_optout", email,
                {"source": source, "scope": scope, "closed_rfqs": [r["id"] for r in rows]})
    return {"email": email, "scope": scope, "closed_rfqs": len(rows)}


# ---------------------------------------------------------------- the one gate for partner e-mail

def sandbox_address(email: str) -> str | None:
    """While the company mailbox does not exist, every partner e-mail goes to a sandbox mailbox derived from the real
    address (info@carrier.fi -> info.carrier-fi@<PARTNER_SANDBOX_DOMAIN>). The real address is logged, never mailed."""
    domain = config.sandbox_domain("PARTNER_SANDBOX_DOMAIN")
    if not domain:
        return None
    local, _, host = email.lower().partition("@")
    return f"{re.sub(r'[^a-z0-9]+', '-', local).strip('-')}.{re.sub(r'[^a-z0-9]+', '-', host).strip('-')}@{domain}"


def partner_sender() -> str:
    return config.env("PARTNER_EMAIL_FROM") or config.EMAIL_FROM


def partner_reply_to() -> str | None:
    return config.env("PARTNER_REPLY_TO") or None


def company_mailbox(email: str | None) -> bool:
    """An address on a company-controlled mail domain (e.g. a marc+tag@ alias): a TEST partner, never an outside company."""
    return config.company_mail_domain(email, partner_sender())


def outreach_live() -> bool:
    return config.env_bool("PARTNER_OUTREACH_LIVE", False)


def outreach_allowed(email: str) -> bool:
    allowed = {x.strip().lower() for x in config.env("PARTNER_OUTREACH_ALLOWLIST").split(",") if x.strip()}
    return outreach_live() or email.strip().lower() in allowed


async def _reserve_send(kind: str, settings: dict, partner: str = "") -> bool:
    # Both caps are checked and incremented in ONE atomic document update. Different lanes or contacts at
    # the same partner cannot race past a cap. Reservations survive provider timeouts (delivery is uncertain).
    limit = settings["partner_daily_send_limit"]
    cap = int(limit * RFQ_SHARE_OF_LIMIT) if kind in ("rfq", "reminder") else limit
    day = helsinki_day()
    try:
        await db.partner_send_counters.update_one({"day": day}, {"$setOnInsert": {"day": day, "count": 0}}, upsert=True)
    except DuplicateKeyError:
        pass
    query, increments = {"day": day, "count": {"$lt": cap}}, {"count": 1}
    if partner:
        field = "partners." + hashlib.sha256(partner.lower().encode()).hexdigest()
        partner_cap = settings.get("partner_per_partner_daily_limit", 4)
        if partner_cap <= 0:
            return False
        query["$or"] = [{field: {"$exists": False}}, {field: {"$lt": partner_cap}}]
        increments[field] = 1
    res = await db.partner_send_counters.update_one(query, {"$inc": increments})
    return res.modified_count == 1


async def sends_today() -> int:
    doc = await db.partner_send_counters.find_one({"day": helsinki_day()}, NO_ID)
    return (doc or {}).get("count", 0)


def _identity(settings: dict) -> tuple[list[str], list[str]]:
    bid = settings.get("seller_business_id") or ""
    fi = [f"{config.COMPANY_NAME}, " + (f"Y-tunnus {bid}" if bid else "perustettava yhtiö (company in formation), Y-tunnus rekisteröinti kesken") +
          f", {config.COMPANY_ADDRESS}. Yhteys: {config.COMPANY_EMAIL}."]
    en = [f"{config.COMPANY_NAME}, " + (f"business ID {bid}" if bid else "company in formation, business ID registration pending") +
          f", {config.COMPANY_ADDRESS}. Contact: {config.COMPANY_EMAIL}."]
    return fi, en


def compose(row: dict, kind: str, settings: dict, amount_cents: int | None = None) -> tuple[str, list[str]]:
    """Bilingual (FI first) RFQ / counteroffer / acceptance / decline / clarification / reminder text."""
    mode, countries = row["lane"].split(":")
    origin, destination = countries.split(">")
    svc_fi, svc_en = SERVICE_TEXT[mode]
    if row.get("request_scope"):
        _, _, svc_fi, svc_en = REQUEST_SERVICES[row["service"]]
        if row.get("equipment_type"):
            svc_fi += " " + row["equipment_type"]
            svc_en += " " + row["equipment_type"]
    basis_fi, basis_en = BASIS_TEXT[row["basis"]]
    eur = (lambda c: f"{c / 100:,.2f}".replace(",", " ").replace(".", ",")) if amount_cents is not None else None
    eur_en = (lambda c: f"{c / 100:,.2f}") if amount_cents is not None else None
    lane_fi = f"{COUNTRY_FI.get(origin, origin)} → {COUNTRY_FI.get(destination, destination)}"
    subject_fi = {"rfq": "Tarjouspyyntö", "reminder": "Muistutus: tarjouspyyntö", "counteroffer": "Vastatarjous",
                  "accept": "Hyväksymme tarjouksenne", "decline": "Kiitos tarjouksesta", "clarify": "Tarkennuspyyntö"}[kind]
    subject_en = {"rfq": "Request for quotation", "reminder": "Reminder: request for quotation", "counteroffer": "Counteroffer",
                  "accept": "We accept your offer", "decline": "Thank you for your offer", "clarify": "Please clarify"}[kind]
    subject = f"[{row['ref']}] {subject_fi} / {subject_en}: {mode} {origin}→{destination}"
    fi, en = [], []
    if kind in ("rfq", "reminder"):
        if kind == "reminder":
            fi.append("Muistutamme alla olevasta tarjouspyynnöstä. Jos reitti ei sovi teille, kertokaa se lyhyesti, niin emme kysy uudelleen.")
            en.append("A short reminder of the request below. If the lane does not suit you, a one-line no is fine and we will not ask again.")
        fi += [f"Pyydämme hintaa reitille {lane_fi}: {svc_fi}. Hinta {basis_fi} euroina, kaikki pakolliset lisät "
               f"(polttoaine, lautat, tietullit, ETS) eriteltyinä tai kokonaishintana.",
               "Vastatkaa tähän viestiin muodossa: Hinta (EUR) · Lisät · Kuljetusaika (päivää) · Voimassa asti.",
               "Haemme pysyvää kumppania tälle reitille: hyväksytty hinta viedään hinnastoomme ja käytämme sitä asiakastarjouksissa."]
        en += [f"Please quote {origin} → {destination}: {svc_en}. Price {basis_en} in EUR, every mandatory surcharge "
               f"(fuel, ferries, tolls, ETS) itemised or included as an all-in price.",
               "Reply to this e-mail with: Price (EUR) · Surcharges · Transit time (days) · Valid until.",
               "We are looking for a regular partner on this lane: an accepted rate goes into our rate table for customer quotes."]
    elif kind == "counteroffer":
        fi.append(f"Kiitos tarjouksestanne. Voisitteko hyväksyä kokonaishinnan {eur(amount_cents)} EUR {basis_fi} "
                  f"(kaikki pakolliset lisät sisältyvät)? Jos hyväksytte, vastatkaa 'hyväksymme', niin vahvistamme hinnan heti.")
        en.append(f"Thank you for your offer. Could you accept EUR {eur_en(amount_cents)} {basis_en} all-in "
                  f"(every mandatory surcharge included)? If yes, reply 'we accept' and we confirm the rate at once.")
    elif kind == "accept":
        fi.append(f"Hyväksymme kokonaishinnan {eur(amount_cents)} EUR {basis_fi}, voimassa {row.get('accepted_valid_to')} asti, "
                  f"kuljetusaika {row.get('accepted_transit_days')} päivää. Hinta on nyt hinnastossamme; tilaukset tulevat "
                  f"tällä viitteellä. Kuljetukset NSAB 2015 -ehdoin.")
        en.append(f"We accept EUR {eur_en(amount_cents)} {basis_en} all-in, valid until {row.get('accepted_valid_to')}, "
                  f"transit {row.get('accepted_transit_days')} days. The rate is now in our rate table; bookings will "
                  f"quote this reference. Carriage under NSAB 2015.")
    elif kind == "decline":
        fi.append("Kiitos tarjouksesta. Hinta on tällä kertaa budjettimme yläpuolella, joten emme jatka tätä kyselyä. "
                  "Pidämme yhteystietonne ja palaamme, kun reitille tulee uusi tarve.")
        en.append("Thank you for your offer. This time it is above our budget, so we close this request. We keep your "
                  "details and come back when the lane opens again.")
    elif kind == "clarify":
        missing = ", ".join(row.get("missing") or []) or "price"
        fi.append(f"Kiitos vastauksesta. Emme saaneet siitä yksiselitteisesti seuraavia tietoja: {missing}. "
                  "Voisitteko vahvistaa kokonaishinnan euroina kaikkine pakollisine lisineen, kuljetusajan ja voimassaolon?")
        en.append(f"Thank you for your reply. We could not read the following unambiguously: {missing}. Could you confirm "
                  "the all-in price in EUR with every mandatory surcharge, the transit time and the validity?")
    if row.get("request_scope"):
        from services.pricing import cargo_summary, note_fi
        cargo = row.get("cargo") or {}
        start, end = cargo.get("origin_location") or {}, cargo.get("destination_location") or {}
        route = f"{start.get('name') or row.get('origin')} ({start.get('country') or origin}) → {end.get('name') or row.get('destination')} ({end.get('country') or destination})"
        summary = cargo_summary(cargo)
        fi += [f"Tarkka reitti: {route}. Tavara: {note_fi(summary)}.",
               "Hinta koskee vain näitä paikkoja ja tätä tavaramäärää, ei koko maiden välistä liikennettä."]
        en += [f"Exact route: {route}. Cargo: {summary}.",
               "This price applies only to these places and this cargo quantity, not to the entire country lane."]
    id_fi, id_en = _identity(settings)
    if row.get("single_rfq_only") and kind == "rfq":
        fi.append("Tämä on alustava, ei-sitova hintatiedustelu, ei kuljetustilaus. Emme lupaa lähetysmääriä.")
        en.append("This is a non-binding indicative rate enquiry, not a transport booking. No shipment volume is promised.")
    source = row.get("contact_source_url") or "julkinen lähde / public source"
    stop = optout_url(row["contact_email"])
    lines = fi + ["—"] + en + ["—",
        *id_fi, f"Löysimme yritysosoitteenne julkisesta lähteestä: {source}. Tämä on yritysten välinen tarjouspyyntö; "
                f"jos ette halua näitä viestejä, poistakaa osoite yhdellä klikkauksella: {stop}",
        *id_en, f"We found your company address in a public source ({source}). This is a business-to-business request; "
                f"to stop these e-mails, one click: {stop}"]
    return subject, lines


async def send_partner_mail(row: dict, kind: str, amount_cents: int | None = None, *, settings: dict | None = None) -> dict:
    """The only way a partner e-mail leaves. Returns {status: sent|logged|failed|held_limit|blocked, reason?}."""
    settings = settings or await get_settings()
    email = row.get("contact_email") or ""
    # A TEST partner on the company's own mailbox runs the real send path with no outside company in reach, so neither
    # the Sourcing switch, single-RFQ mode nor the allowlist holds it; the kill switch still does.
    test_box = bool(row.get("test_mailbox")) and company_mailbox(email)
    if not (await is_on(outreach=row) or (test_box and not kill_env())):
        return {"status": "blocked", "reason": "Sourcing Autopilot is off"}
    if config.env_bool("PARTNER_SINGLE_RFQ_ONLY") and not row.get("single_rfq_only") and not test_box:
        return {"status": "blocked", "reason": "Only the single operator-selected RFQ is enabled"}
    if row.get("single_rfq_only") and kind != "rfq":
        return {"status": "blocked", "reason": "Single RFQ permits no follow-up messages"}
    if not (test_box or outreach_allowed(email)):
        return {"status": "blocked", "reason": "Live outreach awaits owner approval (PARTNER_OUTREACH_LIVE)"}
    if row.get("verification_only") and kind != "rfq":
        return {"status": "blocked", "reason": "Company mailbox verification permits one RFQ only"}
    provider = config.env("PARTNER_EMAIL_PROVIDER") or config.EMAIL_PROVIDER
    if provider != "log" and not partner_reply_to():
        return {"status": "blocked", "reason": "PARTNER_REPLY_TO must name the real inbound mailbox"}
    cand = await db.partner_candidates.find_one({"id": row.get("candidate_id")}, NO_ID) or {"domain": row.get("domain")}
    ok, why = discovery.email_is_b2b(email, cand)
    if not ok:
        return {"status": "blocked", "reason": f"not B2B: {why}"}
    if await is_opted_out(email, cand.get("domain")):
        await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "opted_out", "pending": None}})
        return {"status": "blocked", "reason": "recipient opted out"}
    if not await _reserve_send(kind, settings, row.get("domain") or email):
        await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"pending": {"kind": kind, "amount_cents": amount_cents},
            "status": "queued" if kind in ("rfq",) else row.get("status"), "updated_at": now_iso()}})
        return {"status": "held_limit", "reason": "daily or per-partner sending limit reached"}
    if row.get("single_rfq_only"):
        # Persist before calling the provider. An ambiguous timeout must never cause a second send.
        reserved = await db.partner_outreach.update_one(
            {"id": row["id"], "single_send_reserved_at": {"$exists": False}},
            {"$set": {"single_send_reserved_at": now_iso(), "pending": None}})
        if not reserved.modified_count:
            return {"status": "blocked", "reason": "Single RFQ send already reserved; inspect delivery evidence"}
    subject, lines = compose(row, kind, settings, amount_cents)
    markup, text = render(subject, lines, footer=f"{config.COMPANY_NAME} · {config.COMPANY_ADDRESS}")
    to = sandbox_address(email) or email
    provider = config.env("PARTNER_EMAIL_PROVIDER") or config.EMAIL_PROVIDER
    smtp = smtp_settings("PARTNER_SMTP") if config.env("PARTNER_SMTP_HOST") else None
    stop = optout_url(email)
    headers = {"List-Unsubscribe": f"<{stop}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
               "X-Huolitsija-RFQ": row["ref"]}
    if row.get("last_message_id"):
        headers.update({"In-Reply-To": row["last_message_id"], "References": row["last_message_id"]})
    if to != email:
        headers["X-Huolitsija-Original-To"] = email
    delivery = await send_email(to, subject, markup, text, tag="partner_outreach", provider=provider, smtp=smtp,
                                from_addr=partner_sender(), reply_to=partner_reply_to(), headers=headers,
                                ref={"type": "partner_outreach", "id": row["id"]})
    msg = {"id": new_id(), "direction": "out", "kind": kind, "outreach_id": row["id"], "candidate_id": row.get("candidate_id"),
           "ref": row["ref"], "from": partner_sender(), "to": to, "to_original": email, "sandbox": to != email,
           "subject": subject, "text": text, "amount_cents": amount_cents, "message_id": delivery.get("message_id"),
           "provider_id": delivery.get("provider_id"), "reply_to": partner_reply_to(),
           "email_id": delivery["id"], "provider": delivery.get("provider"), "status": delivery["status"], "at": now_iso(),
           "seq": time.time_ns()}
    await db.partner_messages.insert_one(dict(msg))
    entry = {"kind": kind, "at": now_iso(), "amount_cents": amount_cents, "email_id": delivery["id"],
             "delivery_status": delivery["status"], "message_id": delivery.get("message_id"), "provider_id": delivery.get("provider_id")}
    status = "email_failed" if delivery["status"] == "failed" else \
        {"accept": "accepted", "decline": "declined"}.get(kind, "awaiting_reply")
    sets = {"status": status, "updated_at": now_iso(), "last_outbound_at": now_iso(), "pending": None, "send_to": to}
    if delivery.get("message_id"):
        sets["last_message_id"] = delivery["message_id"]
    if kind == "counteroffer":
        sets["last_counter_cents"] = amount_cents
    await db.partner_outreach.update_one({"id": row["id"]}, {"$push": {"log": entry}, "$set": sets})
    await action("partner_" + kind, "partner_outreach", row["id"], data=entry, reversible=False)
    return {"status": delivery["status"], "message_id": delivery.get("message_id"), "provider_id": delivery.get("provider_id"), "to": to}


# ---------------------------------------------------------------- outreach

def _new_ref() -> str:
    import secrets
    return "RFQ-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))


async def open_rfq(candidate: dict, lane: str, settings: dict, request: dict | None = None, *,
                   single_rfq_only: bool = False) -> dict | None:
    mode = lane.split(":")[0]
    key = f"{candidate['id']}:{lane}"
    if (request or {}).get("request_scope"):
        key += ":" + request["request_scope"]
    if single_rfq_only:
        key = "single-real-rfq-v1"  # one approved send across candidates, scopes and concurrent callers
    service = (request or {}).get("service") or SERVICE[mode]
    basis = REQUEST_SERVICES[service][1] if (request or {}).get("request_scope") else BASIS[mode]
    equipment = (request or {}).get("equipment_type") if (request or {}).get("request_scope") else ("40HC" if mode in ("sea", "rail") else None)
    if await db.partner_outreach.find_one({"campaign_key": key}):
        return None
    row = {"id": new_id(), "ref": _new_ref(), "campaign_key": key, "candidate_id": candidate["id"],
           "carrier_id": candidate.get("carrier_id") or candidate["id"], "carrier_name": candidate["name"],
           "business_id": candidate.get("business_id"), "domain": candidate.get("domain"), "country": candidate.get("country"),
           "verification_only": bool(candidate.get("verification_only")),
           "test_mailbox": bool(candidate.get("test_mailbox")),
           "single_rfq_only": single_rfq_only,
           "lane": lane, "mode": mode, "service": service, "basis": basis,
           "request_scope": (request or {}).get("request_scope"), "cargo": (request or {}).get("cargo") or {},
           "origin": (request or {}).get("origin"), "destination": (request or {}).get("destination"),
           "equipment_type": equipment, "contact_email": candidate["contact_email"],
           "contact_evidence": candidate.get("contact_source_url"), "contact_source_url": candidate.get("contact_source_url"),
           "sources": candidate.get("sources", []), "score": candidate.get("score"),
           "reason": "; ".join(candidate.get("score_reasons") or []), "sell_cents": settings["partner_sell_price_cents"],
           "request_id": (request or {}).get("id"), "limit_override_cents": (request or {}).get("max_cost_cents"),
           "limit_cents": (min(request["max_cost_cents"], settings["partner_max_price_cents"]) if request
                           else cost_limit(settings, lane, settings["partner_sell_price_cents"])), "rounds": 0, "clarifications": 0,
           "reminders": 0, "offers": [], "status": "discovered", "log": [], "created_at": now_iso(), "updated_at": now_iso()}
    try:
        await db.partner_outreach.insert_one(dict(row))
    except DuplicateKeyError:
        return None
    result = await send_partner_mail(row, "rfq", settings=settings)
    await db.partner_candidates.update_one({"id": candidate["id"]}, {"$set": {"status": "verification" if candidate.get("verification_only") else "contacted", "updated_at": now_iso()}})
    return {**row, "send": result}


async def outreach_lane(lane: str, settings: dict, *, discover: bool = True, request: dict | None = None) -> dict:
    if discover:
        disc = await discovery.discover_lane(lane, contact_lookup=published_contact)
    else:
        disc = {"status": "skipped"}
    open_filter = {"lane": lane, "status": {"$in": ["queued", "awaiting_reply", "discovered"]}}
    if (request or {}).get("request_scope"):
        open_filter["request_scope"] = request["request_scope"]
    open_count = await db.partner_outreach.count_documents(open_filter)
    room = max(0, settings["partner_rfqs_per_lane"] - open_count)
    sent = []
    if room:
        cands = await db.partner_candidates.find({"lanes": lane, "status": "contactable"}, NO_ID).sort("score", -1).to_list(room * 3)
        for c in cands:
            if len(sent) >= room or not await is_on():
                break
            if await is_opted_out(c.get("contact_email"), c.get("domain")):
                await db.partner_candidates.update_one({"id": c["id"]}, {"$set": {"status": "opted_out"}})
                continue
            row = await open_rfq(c, lane, await get_settings(), request)
            if row:
                sent.append({"ref": row["ref"], "partner": row["carrier_name"], "status": row["send"]["status"]})
                if row["send"]["status"] == "held_limit":
                    break
    return {"lane": lane, "discovery": disc, "open_before": open_count, "rfqs": sent}


# ---------------------------------------------------------------- inbound

def _addr(value: str | None) -> str:
    from email.utils import parseaddr
    return parseaddr(value or "")[1].lower()


async def find_outreach_for(msg: dict) -> dict | None:
    m = REF_RX.search(f"{msg.get('subject') or ''}\n{msg.get('text') or ''}")
    if m:
        row = await db.partner_outreach.find_one({"ref": m.group(0)}, NO_ID)
        if row:
            return row
    ids = re.findall(r"<[^>]+>", f"{msg.get('in_reply_to') or ''} {msg.get('references') or ''}")
    if ids:
        out = await db.partner_messages.find_one({"direction": "out", "message_id": {"$in": ids}}, NO_ID)
        if out:
            return await db.partner_outreach.find_one({"id": out["outreach_id"]}, NO_ID)
    return None


def _sender_ok(row: dict, sender: str) -> bool:
    allowed = {(row.get("contact_email") or "").lower(), (row.get("send_to") or "").lower()} - {""}
    if sender in allowed:
        return True
    # A colleague at the same company may answer (same registered company domain), never in sandbox mode.
    return not config.sandbox_domain("PARTNER_SANDBOX_DOMAIN") and bool(row.get("domain")) and \
        discovery.base_domain(sender.split("@")[-1]) == row["domain"]


def _auth_ok(msg: dict) -> tuple[bool, str]:
    """Inbound authenticity: the receiving mailbox's Authentication-Results must show DMARC or DKIM pass. Required
    unless PARTNER_INBOUND_REQUIRE_AUTH=false (sandbox mailboxes add no such header)."""
    if config.env("PARTNER_INBOUND_REQUIRE_AUTH", "true").lower() in ("false", "0", "no"):
        return True, "not required (sandbox)"
    if msg.get("source") in ("webhook", "postmark"):
        return True, "provider webhook (signed)"
    ar = (msg.get("auth_results") or "").lower()
    if re.search(r"dmarc=pass|dkim=pass", ar):
        return True, "dmarc/dkim pass"
    return False, "no DMARC/DKIM pass in Authentication-Results"


async def queue_inbound(msg: dict) -> dict:
    """Durably acknowledge before model parsing; provider retries use the same job key."""
    if not msg.get("message_id"):
        raise ValueError("Inbound Message-ID is required")
    key = "partner.inbound:" + hashlib.sha256(msg["message_id"].encode()).hexdigest()
    queued = await enqueue("partner.inbound", {"message": msg}, key=key, source=msg.get("source", "mail"))
    return {"status": queued["status"], "job_id": queued["id"], "message_id": msg["message_id"]}


@job("partner.inbound", timeout=240)
async def process_inbound_job(j: dict) -> dict:
    # Receipts/opt-outs remain ingestible while outbound Autopilot is stopped.
    return await ingest_message(j["payload"]["message"])


async def try_ingest_rfq(raw: bytes, *, provider_id: str | None = None,
                         authenticated_provider: bool = False) -> dict | None:
    """Shared inbound dispatcher: None ONLY for non-RFQ mail. Call after authenticating the provider."""
    from services.partner_mailbox import parse_raw
    msg = parse_raw(raw)
    if provider_id:
        msg["provider_id"] = provider_id
    if authenticated_provider:
        msg["source"] = "postmark"
    # Keep RFQ-shaped unmatched mail in the RFQ triage path, never create a customer from it.
    if not REF_RX.search(f"{msg.get('subject', '')}\n{msg.get('text', '')}") and not await find_outreach_for(msg):
        return None
    return await queue_inbound(msg)


async def ingest_message(msg: dict) -> dict:
    """msg: message_id, from, to, subject, text, in_reply_to, references, auth_results, source (imap|webhook)."""
    mid = (msg.get("message_id") or "").strip()
    if not mid:
        return {"status": "rejected", "reason": "message without Message-ID"}
    existing = await db.partner_messages.find_one({"direction": "in", "message_id": mid}, NO_ID)
    if existing and existing.get("status") not in ("received", "processing_failed"):
        return {"status": "duplicate"}
    sender = _addr(msg.get("from"))
    row = await find_outreach_for(msg)
    doc = {"id": new_id(), "direction": "in", "message_id": mid, "from": sender, "to": _addr(msg.get("to")),
           "provider_id": msg.get("provider_id"),
           "subject": (msg.get("subject") or "")[:300], "text": (msg.get("text") or "")[:20000], "source": msg.get("source"),
           "in_reply_to": msg.get("in_reply_to"), "outreach_id": row["id"] if row else None,
           "ref": row["ref"] if row else None, "candidate_id": row.get("candidate_id") if row else None, "at": now_iso(),
           "seq": time.time_ns()}
    if not row:
        doc["status"] = "unmatched"
    elif not _sender_ok(row, sender):
        doc["status"] = "sender_mismatch"
    else:
        auth_ok, auth_note = _auth_ok(msg)
        doc["auth"] = auth_note
        doc["status"] = "received" if auth_ok else "unauthenticated"
    if existing:
        doc = existing
        doc["status"] = "received"
    else:
        try:
            await db.partner_messages.insert_one(dict(doc))
        except DuplicateKeyError:
            return {"status": "duplicate"}
    if doc["status"] != "received":
        if row:
            await db.partner_outreach.update_one({"id": row["id"]}, {"$push": {"log": {"kind": "reply_ignored", "at": now_iso(),
                "reason": doc["status"], "from": sender}}})
        return {"status": doc["status"], "outreach_id": doc["outreach_id"]}
    await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"last_inbound_at": now_iso(), "updated_at": now_iso(),
        "last_message_id": mid}})
    row = await db.partner_outreach.find_one({"id": row["id"]}, NO_ID)
    try:
        result = await handle_reply(row, doc)
    except Exception:
        await db.partner_messages.update_one({"id": doc["id"]}, {"$set": {"status": "processing_failed"}})
        raise
    await db.partner_messages.update_one({"id": doc["id"]}, {"$set": {"status": result.get("status"),
                                                                     "parsed": result.get("parsed")}})
    return result


async def handle_reply(row: dict, msg: dict) -> dict:
    parsed = await parse_reply(msg["text"], expected={"basis": row["basis"], "equipment_type": row.get("equipment_type"),
                               "lane": row["lane"]}, last_counter_cents=row.get("last_counter_cents"),
                               ref={"type": "partner_outreach", "id": row["id"]})
    parsed.pop("evidence", None)
    entry = {"kind": "reply", "at": now_iso(), "message_id": msg["message_id"], "parsed": parsed}
    await db.partner_outreach.update_one({"id": row["id"]}, {"$push": {"log": entry, "offers": parsed}})
    if parsed.get("intent") == "opt_out":
        await add_optout(row["contact_email"], source=f"reply {msg['message_id']}")
        return {"status": "opted_out", "parsed": parsed}
    if row["status"] in ("accepted", "declined", "opted_out", "cancelled"):
        return {"status": "closed", "parsed": parsed}
    if not await is_on():
        await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "reply_received"}})
        return {"status": "reply_received", "parsed": parsed}
    if parsed.get("intent") == "decline":
        await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "declined_by_partner", "updated_at": now_iso()}})
        await db.partner_candidates.update_one({"id": row.get("candidate_id")}, {"$set": {"status": "declined"}, "$inc": {"declined_count": 1}})
        return {"status": "declined_by_partner", "parsed": parsed}
    if parsed["status"] != "ok":
        if row.get("single_rfq_only"):
            await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "needs_review", "updated_at": now_iso()}})
            return {"status": "needs_review", "parsed": parsed}
        if row.get("clarifications", 0) < 1:
            await db.partner_outreach.update_one({"id": row["id"]}, {"$inc": {"clarifications": 1}})
            row = {**row, "missing": parsed.get("missing") or (["price (two readings differ)"] if parsed["status"] == "conflict" else [])}
            sent = await send_partner_mail(row, "clarify")
            return {"status": "clarification_sent" if sent["status"] in ("sent", "logged") else sent["status"], "parsed": parsed}
        await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "needs_review", "updated_at": now_iso()}})
        return {"status": "needs_review", "parsed": parsed}
    offer = {"cost_cents": parsed["cost_cents"], "base_cents": parsed.get("base_cents"), "surcharges": parsed.get("surcharges") or [],
             "transit_days": parsed.get("transit_days"), "valid_to": parsed.get("valid_to"), "all_in": True,
             "equipment_type": parsed.get("equipment_type"), "message_id": msg["message_id"], "method": parsed.get("method")}
    if parsed.get("intent") == "accept_counter":
        prev = next((o for o in reversed(row.get("offers") or []) if o.get("transit_days") and o.get("valid_to")), {})
        offer["transit_days"] = offer["transit_days"] or prev.get("transit_days")
        offer["valid_to"] = offer["valid_to"] or prev.get("valid_to")
        offer["base_cents"], offer["surcharges"] = None, []
    result = await negotiate(row, offer)
    return {**result, "parsed": parsed}


# ---------------------------------------------------------------- negotiation and rate loading

async def negotiate(row: dict, offer: dict) -> dict:
    s = await get_settings()
    limit = row_limit(s, row)
    price = offer["cost_cents"]
    if type(price) is not int or price <= 0:
        raise HTTPException(400, "Offer purchase price must be positive whole cents")
    if row.get("rate_card_id"):
        return {"status": "closed", "reason": "rate already recorded"}
    max_rounds = s["partner_max_rounds"]
    if price > limit and not row.get("single_rfq_only"):
        if row.get("rounds", 0) >= max_rounds or price > limit * 3:
            await send_partner_mail(row, "decline", settings=s)
            await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "declined", "limit_cents": limit,
                "close_reason": "above ceiling after all rounds" if price <= limit * 3 else "far above ceiling"}})
            return {"status": "declined", "limit_cents": limit}
        round_no = row.get("rounds", 0) + 1
        amount = counter_amount(limit, round_no, max_rounds, s["partner_counter_anchor_pct"])
        amount = max(amount, row.get("last_counter_cents") or 0)  # concessions only move up
        reserved = await db.partner_outreach.update_one({"id": row["id"], "rounds": {"$lt": max_rounds}, "rate_card_id": None},
                                                        {"$inc": {"rounds": 1}, "$set": {"limit_cents": limit}})
        if not reserved.modified_count:
            return {"status": "outside_policy", "limit_cents": limit}
        sent = await send_partner_mail({**row, "rounds": round_no}, "counteroffer", amount, settings=s)
        return {"status": "counteroffered" if sent["status"] in ("sent", "logged") else sent["status"],
                "counter_cents": amount, "round": round_no, "limit_cents": limit}
    try:
        until = date.fromisoformat(offer.get("valid_to") or "")
    except (TypeError, ValueError):
        raise HTTPException(400, "Enter the carrier offer's expiry date")
    days = offer.get("transit_days")
    if until < date.today() or type(days) is not int or not 1 <= days <= 180:
        raise HTTPException(400, "Offer must be current and include transit days (1–180)")
    if row["basis"] == "PER_CONTAINER" and row.get("equipment_type") not in ("20GP", "40GP", "40HC", "45HC", "20RF", "40RF"):
        raise HTTPException(409, "The RFQ must specify container equipment before a rate can be accepted")
    rate = await load_rate(row, offer, limit, s, status="accepted" if await is_on(outreach=row) and not row.get("verification_only")
                           and not row.get("single_rfq_only") else "pending")
    if rate["status"] == "accepted":
        await send_partner_mail({**row, "accepted_valid_to": until.strftime("%d.%m.%Y"), "accepted_transit_days": days},
                                "accept", price, settings=s)
        await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "accepted"}})
    return rate


def _surcharges_for_card(row: dict, offer: dict) -> tuple[int, list[dict]] | None:
    """Base + itemised surcharges in the pricing engine's format, or None when only an all-in total is known."""
    base, sur = offer.get("base_cents"), offer.get("surcharges") or []
    if not base or not sur:
        return None
    unit_basis = {"PER_SHIPMENT": "PER_TRUCK", "PER_CONTAINER": "PER_CONTAINER",
                  "PER_KG": "PER_KG", "PER_WM": "PER_WM"}[row["basis"]]
    out, total = [], base
    for i, s in enumerate(sur):
        code = (s.get("code") or "SURCHARGE").upper()
        code = code if code not in {x["code"] for x in out} else f"{code}{i}"
        if s.get("pct") is not None:
            out.append({"code": code, "label": s.get("label") or code, "basis": "PERCENT", "pct": float(s["pct"])})
            total += int(round(base * float(s["pct"]) / 100))
        elif s.get("amount_cents"):
            out.append({"code": code, "label": s.get("label") or code, "basis": unit_basis, "unit_cost_cents": int(s["amount_cents"])})
            total += int(s["amount_cents"])
        else:
            return None
    return (base, out) if total == offer["cost_cents"] else None


async def ensure_carrier(row: dict) -> str:
    cand = await db.partner_candidates.find_one({"id": row.get("candidate_id")}, NO_ID) or {}
    row = {**row, "mode": row.get("mode") or row["lane"].split(":")[0]}
    cid = cand.get("carrier_id") or row.get("carrier_id") or row.get("candidate_id")
    existing = await db.carriers.find_one({"id": cid}, NO_ID)
    if not existing:
        await db.carriers.insert_one({
            "id": cid, "name": row["carrier_name"], "code": None, "modes": [row["mode"]], "country": row.get("country"),
            "integration": {"type": "email", "status": "configured"}, "contact_email": row["contact_email"],
            "website": cand.get("website"), "business_id": row.get("business_id"), "on_time_pct": None,
            "co2_factor_g_tkm": None, "dg_classes_accepted": [], "dg_un_blocked": [], "dg_note": None,
            "services_note": f"Partner from Sourcing Autopilot ({row['lane']}), RFQ {row['ref']}",
            "services_note_i18n": {"en": f"Partner from Sourcing Autopilot ({row['lane']}), RFQ {row['ref']}",
                                   "fi": f"Kumppani Hankinta-autopilotista ({row['lane']}), tarjouspyyntö {row['ref']}"},
            "source": "partner_network", "sources": row.get("sources", []), "active": True,
            "created_at": now_iso(), "updated_at": now_iso()})
    elif row["mode"] not in existing.get("modes", []):
        await db.carriers.update_one({"id": cid}, {"$addToSet": {"modes": row["mode"]}})
    await db.partner_candidates.update_one({"id": row.get("candidate_id")}, {"$set": {"status": "partner", "carrier_id": cid}})
    return cid


async def load_rate(row: dict, offer: dict, limit: int, s: dict, *, status: str) -> dict:
    mode, countries = row["lane"].split(":")
    origin, destination = countries.split(">")
    # A company mailbox check is not a transport supplier. Keep its parsed card archived without creating a carrier.
    # A TEST partner (company mailbox) likewise: its card is kept archived, so production pricing never uses it.
    carrier_id = None if row.get("verification_only") or row.get("test_mailbox") else await ensure_carrier(row)
    lane_req = await db.lane_rate_requests.find_one({"id": row["request_id"]}, NO_ID) if row.get("request_id") else None
    s5_ids = (lane_req or {}).get("rate_request_ids") or []
    price = offer["cost_cents"]
    split = _surcharges_for_card(row, offer)
    unit, surcharges = split if split else (price, [])
    rate = {"id": new_id(), "carrier_id": carrier_id, "mode": mode, "service": row["service"], "basis": row["basis"],
            "service_name": f"{row['carrier_name']} · {SERVICE_LABEL[mode]} {origin}→{destination}",
            "origin": {"country": origin}, "destination": {"country": destination}, "via": [],
            "unit_cost_cents": unit, "min_cost_cents": None, "container_costs": None, "tiers": None, "distance_bands": None,
            "all_in_cost_cents": price, "all_in": offer.get("all_in"),
            "currency": "EUR", "transit_days": offer["transit_days"],
            "departure_weekdays": [0, 1, 2, 3, 4] if mode == "road" else [],
            "valid_from": date.today().isoformat(), "valid_to": offer["valid_to"], "status": status,
            "source": "partner_network", "outreach_id": row["id"], "surcharges": surcharges, "archived": bool(row.get("verification_only") or row.get("test_mailbox")),
            "verification_only": bool(row.get("verification_only")), "test_mailbox": bool(row.get("test_mailbox")),
            "source_detail": {"partner": row["carrier_name"], "business_id": row.get("business_id"), "ref": row["ref"],
                              "outreach_id": row["id"], "message_id": offer.get("message_id"), "parsed_by": offer.get("method"),
                              "source_url": row.get("contact_source_url"), "rounds": row.get("rounds", 0),
                              "lane_request_id": row.get("request_id"), "rate_request_id": s5_ids[0] if s5_ids else None,
                              "rate_request_ids": s5_ids},
            "notes": f"Negotiated by Sourcing Autopilot, RFQ {row['ref']}" + (f", {row.get('rounds')} counteroffer round(s)" if row.get("rounds") else ""),
            "notes_i18n": {"en": f"Negotiated by Sourcing Autopilot, RFQ {row['ref']}" + (f", {row.get('rounds')} counteroffer round(s)" if row.get("rounds") else ""),
                           "fi": f"Hankinta-autopilotin neuvottelema, tarjouspyyntö {row['ref']}" + (f", vastatarjouskierroksia {row.get('rounds')}" if row.get("rounds") else "")},
            "created_at": now_iso(), "updated_at": now_iso(), "updated_by": {"type": "system", "name": "Sourcing Autopilot"},
            "policy": {"limit_cents": limit, "sell_cents": row["sell_cents"], "target_margin_pct": s["partner_target_margin_pct"],
                       "max_price_cents": s["partner_max_price_cents"]}}
    if row.get("request_scope"):
        from services.pricing import cargo_metrics
        cargo = row.get("cargo") or {}
        for side in ("origin", "destination"):
            point = cargo.get(side + "_location") or {}
            rate[side] = {**rate[side], **{k: point[k] for k in ("unlocode", "name", "country", "lat", "lon") if point.get(k) is not None},
                          "radius_km": 1}
            if not point and len(row.get(side) or "") == 5:
                rate[side]["unlocode"] = row[side]
        rate["request_scope"] = row["request_scope"]
        metrics = cargo_metrics(cargo)
        rate["cargo_scope"] = {k: metrics.get(k) for k in ("weight_kg", "volume_m3", "ldm", "containers", "is_fcl")}
        rate["service_name"] = f"{row['carrier_name']} · {row['service']} {row.get('origin')}→{row.get('destination')}"
    if row["basis"] == "PER_CONTAINER":
        rate["container_costs"] = {row["equipment_type"]: unit}
    try:
        await db.rate_cards.insert_one(dict(rate))
    except DuplicateKeyError:
        raise HTTPException(409, "A rate card is already recorded for this RFQ")
    await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": status, "rate_card_id": rate["id"],
        "accepted_cost_cents": price, "updated_at": now_iso()}, "$push": {"log": {"kind": "rate_" + status, "at": now_iso(),
        "amount_cents": price, "rate_card_id": rate["id"], "valid_to": offer["valid_to"]}}})
    await action("partner_rate_" + status, "rate_card", rate["id"], data={"outreach_id": row["id"], "ref": row["ref"],
                 "limit_cents": limit, "cost_cents": price}, reversible=True)
    rate.pop("_id", None)
    if row.get("request_id") and status == "accepted":
        req = await db.lane_rate_requests.find_one_and_update(
            {"id": row["request_id"]}, {"$set": {"status": "rate_loaded", "updated_at": now_iso()},
                                        "$addToSet": {"rate_card_ids": rate["id"]}}, projection=NO_ID, return_document=True)
        if req:
            await emit("partner.rate_loaded", {"request": req, "rate_card": rate}, background=True)
    if status == "accepted":  # s5 resolves its rate_requests from this (and re-checks every 60 s)
        await emit("rate_card.accepted", {"rate_card": rate}, background=True)
    return rate


async def process_offer(outreach: dict, offer: dict, actor=None):
    """Operator-recorded or parsed offer. With the switch off a person may still record an offer: it becomes a pending
    rate (never a price in quotes) and no e-mail is sent."""
    s = await get_settings()
    if actor is None and not await is_on(outreach=outreach):
        raise HTTPException(409, "Sourcing Autopilot is off")
    if not await is_on(outreach=outreach):
        limit = cost_limit(s, outreach["lane"], outreach.get("sell_cents"))
        price = offer.get("cost_cents")
        if type(price) is not int or price <= 0:
            raise HTTPException(400, "Offer purchase price must be positive whole cents")
        if price > limit:
            raise HTTPException(409, "Offer exceeds the current partner policy")
        try:
            until = date.fromisoformat(offer.get("valid_to") or "")
        except (TypeError, ValueError):
            raise HTTPException(400, "Enter the carrier offer's expiry date")
        if until < date.today() or type(offer.get("transit_days")) is not int or not 1 <= offer["transit_days"] <= 180:
            raise HTTPException(400, "Offer must be current and include transit days (1–180)")
        return await load_rate({**outreach, "ref": outreach.get("ref") or outreach["id"][:8]}, {**offer, "all_in": True},
                               limit, s, status="pending")
    return await negotiate({**outreach, "ref": outreach.get("ref") or outreach["id"][:8]}, {**offer, "all_in": True})


# ---------------------------------------------------------------- cycle, follow-ups, kill switch, status

async def follow_ups(settings: dict) -> list[dict]:
    out = []
    cutoff_remind = (now() - timedelta(days=REMIND_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_expire = (now() - timedelta(days=EXPIRE_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    async for row in db.partner_outreach.find({"status": "awaiting_reply", "last_outbound_at": {"$lt": cutoff_remind}}, NO_ID):
        if row.get("single_rfq_only"):
            continue
        if row.get("last_inbound_at", "") > row.get("last_outbound_at", ""):
            continue
        if row.get("reminders", 0) == 0 and row["log"] and row["log"][-1]["kind"] == "rfq":
            await db.partner_outreach.update_one({"id": row["id"]}, {"$inc": {"reminders": 1}})
            out.append({"ref": row["ref"], "reminder": (await send_partner_mail(row, "reminder", settings=settings))["status"]})
        elif row.get("last_outbound_at", "") < cutoff_expire:
            await db.partner_outreach.update_one({"id": row["id"]}, {"$set": {"status": "expired", "updated_at": now_iso()}})
            out.append({"ref": row["ref"], "expired": True})
    async for row in db.partner_outreach.find({"pending": {"$ne": None}, "status": {"$nin": ["cancelled", "opted_out"]}}, NO_ID):
        if row.get("single_rfq_only"):
            continue
        if not row.get("pending"):
            continue
        res = await send_partner_mail(row, row["pending"]["kind"], row["pending"].get("amount_cents"), settings=settings)
        out.append({"ref": row["ref"], "resent": row["pending"]["kind"], "status": res["status"]})
        if res["status"] == "held_limit":
            break
    return out


def _country(place: str) -> str:
    place = (place or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}([A-Z0-9]{3})?", place):
        raise HTTPException(400, "Give origin and destination as ISO country (FI) or UN/LOCODE (FIHEL)")
    return place[:2]


async def request_lane_rates(origin: str, destination: str, mode: str, cargo: dict | None = None, *,
                             max_cost_cents: int | None = None, quote_id: str | None = None, requested_by: str | None = None,
                             rate_request_id: str | None = None, service: str | None = None) -> dict:
    """On-demand sourcing for a lane without rates (s5 global quoting). Returns the request document at once; the
    work runs in the cycle (and starts immediately when the switch is on). Result: the accepted rate card is in
    rate_cards (status accepted, source partner_network), lane_rate_requests[id].status becomes "rate_loaded" with
    rate_card_ids, and the event "partner.rate_loaded" {request, rate_card} is emitted.
    Status values: open | working | rfqs_sent | rate_loaded | no_candidates | off."""
    if mode not in SERVICE:
        raise HTTPException(400, "mode must be road, sea, air or rail")
    if max_cost_cents is not None and (type(max_cost_cents) is not int or max_cost_cents <= 0):
        raise HTTPException(400, "max_cost_cents must be a positive whole number of cents")
    s = await get_settings()
    lane = f"{mode}:{_country(origin)}>{_country(destination)}"
    ceiling = min(int(max_cost_cents), s["partner_max_price_cents"]) if max_cost_cents else \
        cost_limit({**s, "partner_approved_lanes": [lane]}, lane, s["partner_sell_price_cents"])
    scope, equipment = None, None
    if service is not None:
        if service not in REQUEST_SERVICES or REQUEST_SERVICES[service][0] != mode:
            raise HTTPException(400, "Service does not match the transport mode")
        cargo = dict(cargo or {})
        if REQUEST_SERVICES[service][1] == "PER_CONTAINER":
            equipment = cargo.get("equipment_type")
            if equipment not in ("20GP", "40GP", "40HC", "45HC", "20RF", "40RF"):
                raise HTTPException(400, "Specify the requested container equipment")
        scope = hashlib.sha256(json.dumps({"origin": origin.upper(), "destination": destination.upper(),
            "service": service, "equipment": equipment, "cargo": cargo}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    open_req = await db.lane_rate_requests.find_one({"lane": lane, "request_scope": scope,
                  "status": {"$in": ["open", "working", "rfqs_sent"]}}, NO_ID)
    if open_req:  # one sourcing effort per lane; later callers share it
        add = {**({"quote_ids": quote_id} if quote_id else {}), **({"rate_request_ids": rate_request_id} if rate_request_id else {})}
        if add:
            await db.lane_rate_requests.update_one({"id": open_req["id"]}, {"$addToSet": add})
        return {**open_req, "shared": True}
    req = {"id": new_id(), "lane": lane, "origin": origin.upper(), "destination": destination.upper(), "mode": mode,
           "cargo": cargo or {}, "request_scope": scope, "service": service, "equipment_type": equipment, "max_cost_cents": ceiling, "quote_ids": [quote_id] if quote_id else [],
           "requested_by": requested_by, "status": "manual" if config.env_bool("PARTNER_SINGLE_RFQ_ONLY") else
           ("open" if await is_on() else "off"), "rate_card_ids": [],
           "rate_request_ids": [rate_request_id] if rate_request_id else [],
           "created_at": now_iso(), "updated_at": now_iso()}
    await db.lane_rate_requests.insert_one(dict(req))
    await audit({"id": requested_by or "system", "name": requested_by or "system"}, "partner_lane_request", "lane_rate_request",
                req["id"], {"lane": lane, "max_cost_cents": ceiling, "quote_id": quote_id})
    if req["status"] == "open":  # fast path; the durable cycle job picks it up if this process stops first
        asyncio.get_running_loop().create_task(_work_request(req["id"]))
    return req


async def _work_request(request_id: str) -> dict:
    if config.env_bool("PARTNER_SINGLE_RFQ_ONLY"):
        return {"status": "manual", "reason": "Single RFQ mode requires operator selection"}
    if not await is_on():
        return {"status": "off"}
    # claim it: the immediate task and the cycle must not work the same request twice; a claim left by a crashed process
    # (still "working" after STALE_WORK_MINUTES) is taken over
    stale = (now() - timedelta(minutes=STALE_WORK_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    req = await db.lane_rate_requests.find_one_and_update({"id": request_id, "$or": [
                                                              {"status": {"$in": ["open", "off"]}},
                                                              {"status": "working", "updated_at": {"$lt": stale}}]},
                                                          {"$set": {"status": "working", "updated_at": now_iso()}},
                                                          projection=NO_ID, return_document=True)
    if not req:
        return {"status": "busy_or_done"}
    try:
        res = await outreach_lane(req["lane"], await get_settings(), request=req)
    except Exception as exc:  # noqa: BLE001 - retried by the next cycle
        log.exception("lane request %s failed", request_id)
        await db.lane_rate_requests.update_one({"id": request_id, "status": "working"}, {"$set": {"status": "open"}})
        return {"status": "error", "error": str(exc)[:200]}
    sent = [r for r in res["rfqs"] if r["status"] in ("sent", "logged")]
    status = "rfqs_sent" if sent else ("no_candidates" if not res["rfqs"] and not res["open_before"] else "open")
    await db.lane_rate_requests.update_one({"id": request_id, "status": "working"},
                                           {"$set": {"status": status, "updated_at": now_iso(), "rfqs": res["rfqs"]}})
    if sent and req.get("rate_request_ids"):
        await db.rate_requests.update_many({"id": {"$in": req["rate_request_ids"]}, "status": "open"},
                                           {"$set": {"status": "sourcing", "lane_request_id": request_id}})
    return {"status": status, **res}


async def import_rate_requests() -> list[dict]:
    """s5's demand queue (collection rate_requests, owned by s5): every open doc is approved sourcing demand for its lane
    ("sea:NG>PE"), ceiling max_cost_cents. It is folded into one lane_rate_requests effort per lane; the doc goes to
    "sourcing" when an RFQ is out, and the accepted card carries source_detail.rate_request_id."""
    out = []
    async for rr in db.rate_requests.find({"status": "open", "lane_request_id": None}, NO_ID).limit(50):
        try:
            mode, pair = (rr.get("lane") or "").split(":")
            origin, destination = pair.split(">")
            req = await request_lane_rates(origin, destination, mode, rr.get("cargo"), max_cost_cents=rr.get("max_cost_cents"),
                                           requested_by="global_quote", rate_request_id=rr["id"])
        except (ValueError, HTTPException) as exc:
            log.warning("rate_request %s skipped: %s", rr.get("id"), exc)
            continue
        if rr.get("quote_ids"):
            await db.lane_rate_requests.update_one({"id": req["id"]}, {"$addToSet": {"quote_ids": {"$each": rr["quote_ids"]}}})
        await db.rate_requests.update_one({"id": rr["id"]}, {"$set": {"lane_request_id": req["id"]}})
        out.append({"rate_request_id": rr["id"], "lane_request_id": req["id"], "lane": req["lane"]})
    return out


async def expected_reply_hours(mode: str, lane: str | None = None, *, days: int = 60, min_samples: int = 5) -> dict | None:
    """Honest ETA for s5: hours from the RFQ e-mail to the accepted rate, over the last `days`, for the lane when it has
    enough samples, else the mode. None when fewer than `min_samples` (the caller shows its labelled defaults)."""
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def hours(q: dict) -> list[float]:
        out = []
        async for row in db.partner_outreach.find({**q, "status": "accepted", "updated_at": {"$gte": since}}, {"log": 1}):
            sent = next((e["at"] for e in row.get("log") or [] if e.get("kind") == "rfq"), None)
            done = next((e["at"] for e in row.get("log") or [] if e.get("kind") == "rate_accepted"), None)
            if sent and done:
                out.append((datetime.fromisoformat(done) - datetime.fromisoformat(sent)).total_seconds() / 3600)
        return sorted(out)
    for q in ([{"lane": lane}] if lane else []) + [{"lane": {"$regex": f"^{re.escape(mode)}:"}}]:
        xs = await hours(q)
        if len(xs) >= min_samples:
            pick = lambda f: round(xs[min(len(xs) - 1, int(f * len(xs)))], 1)  # noqa: E731
            return {"p50_hours": pick(0.5), "p90_hours": pick(0.9), "samples": len(xs), "scope": "lane" if "$regex" not in str(q) else "mode"}
    return None


async def get_lane_request(request_id: str) -> dict | None:
    return await db.lane_rate_requests.find_one({"id": request_id}, NO_ID)


_cycle_lock = asyncio.Lock()


async def run_cycle(actor=None, *, discover: bool = True) -> dict:
    """One cycle at a time per process: "Run now" during the loop's cycle waits for it, then finds the RFQs it sent."""
    async with _cycle_lock:
        return await _run_cycle(actor, discover=discover)


async def _run_cycle(actor=None, *, discover: bool = True) -> dict:
    started = now_iso()
    inbox = await poll_inbox()
    s = await get_settings()
    if not await is_on():
        return {"status": "off", "inbox": inbox}
    if config.env_bool("PARTNER_SINGLE_RFQ_ONLY"):
        return {"status": "manual", "inbox": inbox, "reason": "Single RFQ mode; no campaigns or follow-ups"}
    if not s["partner_approved_lanes"]:
        return {"status": "needs_policy", "reason": "Configure approved lanes and a price limit", "inbox": inbox}
    lanes = []
    for lane in s["partner_approved_lanes"]:
        if not await is_on():
            break
        try:
            lanes.append(await outreach_lane(lane, s, discover=discover))
        except Exception as exc:  # noqa: BLE001 - one broken lane must not stop the others
            log.exception("lane %s failed", lane)
            lanes.append({"lane": lane, "error": f"{type(exc).__name__}: {exc}"[:300]})
    imported = await import_rate_requests()
    stale = (now() - timedelta(minutes=STALE_WORK_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    requests = [await _work_request(r["id"]) async for r in db.lane_rate_requests.find(
        {"$or": [{"status": {"$in": ["open", "off"]}}, {"status": "working", "updated_at": {"$lt": stale}}]}, {"id": 1})]
    follow = await follow_ups(s) if await is_on() else []
    summary = {"status": "ran", "started_at": started, "finished_at": now_iso(), "inbox": inbox, "lanes": lanes,
               "lane_requests": [{k: r.get(k) for k in ("status", "error")} for r in requests], "imported": imported,
               "follow_ups": follow, "sends_today": await sends_today(), "limit": s["partner_daily_send_limit"]}
    await db.system.update_one({"key": "partner_network_last_cycle"}, {"$set": {"key": "partner_network_last_cycle",
                               "summary": summary}}, upsert=True)
    await audit(actor or {"id": "sourcing", "name": "Sourcing Autopilot"}, "partner_cycle", "settings", "partner_network",
                {"rfqs": sum(len(x.get("rfqs", [])) for x in lanes), "inbox": inbox.get("processed", 0)})
    return summary


run_campaign = run_cycle  # earlier name


async def stop(actor: dict) -> dict:
    """Kill switch: off now, cancel everything queued, audit. Turning it back on needs the typed 'enable' again."""
    before = (await get_settings())["partner_network_enabled"]
    await update_settings({"partner_network_enabled": False})
    res = await db.partner_outreach.update_many({"$or": [{"status": "queued"}, {"pending": {"$ne": None}}]},
                                                {"$set": {"status": "cancelled", "pending": None, "updated_at": now_iso()}})
    await db.settings.update_one({"key": "app"}, {"$set": {"partner_network_enabled_changed": {
        "at": now_iso(), "by": {"id": actor["id"], "name": actor.get("name")}, "enabled": False, "kill": True}}})
    await audit(actor, "partner_network_killed", "settings", "app", {"before": before, "cancelled": res.modified_count})
    return {"enabled": False, "cancelled": res.modified_count}


async def status() -> dict:
    s = await get_settings()
    last = await db.system.find_one({"key": "partner_network_last_cycle"}, NO_ID)
    counts = {x["_id"]: x["n"] async for x in db.partner_outreach.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
    return {"enabled": await is_on(), "switch": s["partner_network_enabled"], "env_kill": kill_env(),
            "sends_today": await sends_today(), "limit": s["partner_daily_send_limit"], "rfqs": counts,
            "candidates": await db.partner_candidates.count_documents({}),
            "contactable": await db.partner_candidates.count_documents({"status": "contactable"}),
            "optouts": await db.partner_optouts.count_documents({}),
            "accepted_rates": await db.rate_cards.count_documents({"source": "partner_network", "status": "accepted"}),
            "last_cycle": (last or {}).get("summary"), "inbox": mailbox_config(),
            "sandbox_domain": config.sandbox_domain("PARTNER_SANDBOX_DOMAIN") or None,
            "outreach_live": outreach_live(), "per_partner_limit": s["partner_per_partner_daily_limit"],
            "reply_to": partner_reply_to(),
            "email_provider": config.env("PARTNER_EMAIL_PROVIDER") or config.EMAIL_PROVIDER}


def mailbox_config() -> dict:
    return {"host": config.env("PARTNER_IMAP_HOST") or None, "user": config.env("PARTNER_IMAP_USER") or None,
            "folder": config.env("PARTNER_IMAP_FOLDER", "INBOX"), "configured": bool(config.env("PARTNER_IMAP_HOST") or config.env("PARTNER_INBOUND_SECRET")),
            "provider": "postmark" if config.env("PARTNER_INBOUND_SECRET") else "imap"}


async def poll_inbox() -> dict:
    if not config.env("PARTNER_IMAP_HOST"):
        return {"status": "not_configured", "processed": 0}
    from services.partner_mailbox import fetch_unseen, mark_seen
    try:
        messages = await fetch_unseen()
    except Exception as exc:  # noqa: BLE001 - a mailbox outage is reported, not fatal
        log.warning("partner inbox poll failed: %s", exc)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300], "processed": 0}
    results = []
    for m in messages:
        try:
            results.append(await ingest_message(m))
        except Exception as exc:  # noqa: BLE001
            log.exception("partner reply failed")
            results.append({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
    done = [m["imap_uid"] for m, r in zip(messages, results) if r.get("status") != "error"]
    try:
        await mark_seen(done)
    except Exception as exc:  # noqa: BLE001 - unseen again next poll; Message-ID dedupe makes that harmless
        log.warning("marking partner replies seen failed: %s", exc)
    return {"status": "ok", "processed": len(results), "results": results}


RETENTION_DAYS = 730  # privacy notice: contact and message records 24 months after the last contact


async def retention() -> dict:
    """Once a day: drop partner messages and never-converted candidates older than RETENTION_DAYS. The opt-out list
    and accepted rate cards stay (opt-outs must be remembered; rates follow accounting/customs retention)."""
    day = helsinki_day()
    marker = await db.system.find_one({"key": "partner_retention"}, NO_ID)
    if marker and marker.get("day") == day:
        return {"status": "done_today"}
    cutoff = (now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    msgs = await db.partner_messages.delete_many({"at": {"$lt": cutoff}})
    cands = await db.partner_candidates.delete_many({"updated_at": {"$lt": cutoff}, "status": {"$nin": ["partner", "opted_out"]}})
    await db.system.update_one({"key": "partner_retention"}, {"$set": {"key": "partner_retention", "day": day,
                               "messages": msgs.deleted_count, "candidates": cands.deleted_count}}, upsert=True)
    return {"status": "ran", "messages": msgs.deleted_count, "candidates": cands.deleted_count}


@job("sourcing.cycle", max_attempts=3, timeout=900, label_fi="Hankinta-autopilotin kierros",
     label_en="Sourcing Autopilot cycle")
async def cycle_job(j=None):
    """Every 5 minutes as a durable job (retries with backoff, then dead letter + owner alert). Not an agent job:
    the inbox is read even with the switch off or Autopilot halted, so opt-outs and replies are never missed; every
    partner e-mail still passes send_partner_mail's gate."""
    if await is_on():
        res = await run_cycle()
        out = {k: res.get(k) for k in ("status", "sends_today", "limit")}
        out["rfqs"] = sum(len(x.get("rfqs") or []) for x in res.get("lanes") or [])
    else:
        out = {"status": "off", "inbox": (await poll_inbox()).get("status")}
    out["retention"] = (await retention()).get("status")
    return out


schedule("sourcing.cycle_every_5_min", job="sourcing.cycle", every_seconds=300,
         label_fi="Hankinta-autopilotti 5 minuutin välein", label_en="Sourcing Autopilot every 5 minutes")()
