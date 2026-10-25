"""Autonomous B2B prospecting agent: find -> score -> enrich -> contact, plus follow-ups and reply handling.

Find:    PRH/YTJ open data v3 by TOL 2008 main line of business (settings crm_target_industries), active companies only
         (no ended name, no liquidation / bankruptcy / restructuring, no sole traders), not already a lead or customer.
Score:   Eurostat Comext trade of the CN chapters the industry ships (adapters.trade_stats) + register facts.
Enrich:  the website the company registered in YTJ (never guessed), homepage + contact page, role addresses only.
Contact: template e-mail (Finnish for FI companies, English otherwise), List-Unsubscribe + one-click (RFC 8058) and a
         visible opt-out link. One first touch per company, follow-ups after crm_follow_up_days up to crm_max_follow_ups,
         crm_daily_send_limit per Helsinki day, opt-outs by address and domain.
Replies: IMAP (CRM_IMAP_*), matched by In-Reply-To / References / Message-ID token, intent from core.llm.llm_json
         (fixed enum) or a keyword fallback; unsubscribe -> opt-out, interested -> qualified (+ lane note for quoting).

Autonomy: sends only when Autopilot is on and not halted (s2 decide(agent="sales", action="prospect_email") when
present, else core.autonomy.action + audit). Off: leads are found and scored, drafts wait for a person
(POST /api/crm/leads/{id}/contact). Halted (autopilot_halted): the scheduled agent does nothing and sends nothing.

Env: CRM_EMAIL_PROVIDER (smtp|log|postmark|resend|smtp2go; default PARTNER_EMAIL_PROVIDER -> EMAIL_PROVIDER), CRM_EMAIL_FROM,
CRM_REPLY_TO, SMTP_* (or CRM_SMTP_*), CRM_SANDBOX_DOMAIN (every recipient rewritten to the sandbox; go-live = unset),
CRM_IMAP_HOST/PORT/USER/PASSWORD/SECURITY/FOLDER, CRM_POLL_SECONDS (60), CRM_PROSPECT_DAILY_AT (08:30 Helsinki),
CRM_FIND_PER_CYCLE (20), CRM_ENRICH_PER_CYCLE (10), CRM_OPTOUT_SECRET, CRM_PUBLIC_BASE_URL.
"""
import asyncio
import email as email_lib
import imaplib
import inspect
import logging
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from html import unescape

from pymongo.errors import DuplicateKeyError

from adapters import prh_opendata as prh
from adapters import trade_stats, web_contacts
from core import config
from core.audit import audit
from core.autonomy import action
from core.db import db
from core.email import render
from core.approvals import request_approval
from core.events import emit, on
from core.lifecycle import background_loop
from core.settings import get_settings
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso
from services import crm

try:  # s2 agent runtime (not merged yet on every branch)
    from core.agent_runtime import decide
except ImportError:  # pragma: no cover - depends on the branch
    decide = None
try:
    from core.jobs import job as _job, schedule as _schedule
except ImportError:  # pragma: no cover
    _job = _schedule = None

log = logging.getLogger("prospecting")
INTENTS = ["interested", "not_now", "not_interested", "unsubscribe", "wrong_person", "out_of_office", "question"]
MODES = ["road", "sea", "air", "rail", "unknown"]
TOKEN_RX = re.compile(r"crm\.(L-[A-Z0-9]{6})\.", re.I)
_state: dict = {"last_poll": None, "last_poll_error": None, "last_follow_ups": None}


# ---------------------------------------------------------------- switches

async def halted() -> bool:
    raw = await db.settings.find_one({"key": "app"}, {"_id": 0, "autopilot_halted": 1}) or {}
    return bool(raw.get("autopilot_halted"))


async def autopilot_on(settings: dict | None = None) -> bool:
    s = settings or await get_settings()
    return bool(s.get("autopilot_enabled")) and not await halted()


def _int_env(name: str, default: int) -> int:
    try:
        return int(config.env(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- scoring

def _years(registered: str | None) -> float | None:
    d = parse_iso(registered) if registered else None
    return (now() - d).days / 365.25 if d else None


def register_reasons(lead: dict, settings: dict) -> list[dict]:
    out = []
    years = _years(lead.get("registered"))
    if years is not None:
        pts = 15 if years >= 10 else 8 if years >= 3 else 0
        out.append({"code": "company_age", "points": pts, "fi": f"Rekisteröity {lead['registered']} ({years:.0f} v)",
                    "en": f"Registered {lead['registered']} ({years:.0f} years)"})
    if lead.get("employer_register"):
        out.append({"code": "employer", "points": 10, "fi": "Työnantajarekisterissä (henkilöstöä)",
                    "en": "In the employer register (has staff)"})
    if lead.get("vat_registered"):
        out.append({"code": "vat", "points": 5, "fi": "Arvonlisäverorekisterissä", "en": "VAT registered"})
    if lead.get("website"):
        out.append({"code": "website", "points": 10, "fi": f"Verkkosivu YTJ:ssä: {lead['website']}",
                    "en": f"Website registered in YTJ: {lead['website']}"})
    regions = [r.lower() for r in settings.get("crm_target_regions") or []]
    city = ((lead.get("address") or {}).get("city") or "").lower()
    if regions and city and city in regions:
        out.append({"code": "region", "points": 10, "fi": f"Kohdealueella ({city.title()})", "en": f"In a target region ({city.title()})"})
    role = lead.get("primary_role")
    if role:
        pts = {"logistics": 15, "purchasing": 15, "export": 12, "import": 12, "sales": 10, "orders": 10,
               "customer_service": 7}.get(role, 5)
        out.append({"code": "role_address", "points": pts, "fi": f"Julkaistu rooliosoite ({role}): {lead.get('primary_email')}",
                    "en": f"Published role address ({role}): {lead.get('primary_email')}"})
    return out


async def score_lead(lead: dict, settings: dict) -> dict:
    trade = await trade_stats.industry_score(lead.get("tol_code") or "")
    reasons = trade["reasons"] + register_reasons(lead, settings)
    score = min(100, sum(r["points"] for r in reasons))
    sets = {"score": score, "score_reasons": reasons, "trade": {k: trade.get(k) for k in ("points", "chapters", "total_eur",
            "period", "top_lane")}, "scored_at": now_iso(), "updated_at": now_iso()}
    if lead.get("status") == "new":
        sets["status"] = "scored"
    await db.crm_leads.update_one({"id": lead["id"]}, {"$set": sets})
    return {**lead, **sets}


# ---------------------------------------------------------------- find (PRH)

async def _known(bid: str) -> bool:
    return bool(await db.crm_leads.find_one({"business_id": bid}, {"_id": 1}) or
                await db.companies.find_one({"business_id": bid}, {"_id": 1}))


async def find_leads(settings: dict, max_new: int, notes: list[str]) -> list[dict]:
    configured = [str(t)[:4] for t in settings.get("crm_target_industries") or [] if re.fullmatch(r"\d{4,5}", str(t))]
    industries = list(dict.fromkeys(prh.current_code(t) for t in configured))
    moved = {t: prh.current_code(t) for t in configured if prh.current_code(t) != t}
    if moved:
        notes.append("TOL 2008 -> TOL 2025 (PRH codes since 2026-01-01): " + ", ".join(f"{a}->{b}" for a, b in moved.items()))
    regions = settings.get("crm_target_regions") or [None]
    created: list[dict] = []
    per_industry = max(1, -(-max_new // max(1, len(industries))))
    for tol in industries:
        for region in regions:
            if len(created) >= max_new:
                return created
            key = f"crm:prh_cursor:{tol}:{region or '*'}"
            cur = await db.system.find_one({"key": key}, NO_ID) or {}
            page = int(cur.get("page") or 1)
            try:
                res = await prh.search(tol, page=page, location=region)
            except prh.PRHUnavailable as exc:
                notes.append(str(exc))
                return created
            total, companies = res["total"], res["companies"]
            nxt = page + 1 if page * prh.PAGE_SIZE < total else 1
            await db.system.update_one({"key": key}, {"$set": {"key": key, "page": nxt, "total": total, "at": now_iso(),
                                                               "url": res["url"]}}, upsert=True)
            skipped: dict[str, int] = {}
            # companies that published a website first: only those can be enriched without guessing
            companies.sort(key=lambda c: not prh.website((c.get("website") or {}).get("url")))
            taken = 0
            for c in companies:
                if taken >= per_industry or len(created) >= max_new:
                    break
                if not prh.matches(c, tol):
                    skipped["other_code"] = skipped.get("other_code", 0) + 1
                    continue
                why = prh.skip_reason(c)
                if why:
                    skipped[why.split(" ")[0]] = skipped.get(why.split(" ")[0], 0) + 1
                    continue
                data = prh.normalise(c, tol, res["url"])
                where = data["address"].get("postal_code") if re.fullmatch(r"\d{5}", region or "") else data["address"].get("city")
                if region and (where or "").lower() != region.lower():
                    skipped["outside_region"] = skipped.get("outside_region", 0) + 1
                    continue
                if await _known(data["business_id"]):
                    skipped["known"] = skipped.get("known", 0) + 1
                    continue
                data["source"]["fetched_at"] = now_iso()
                try:
                    lead = await crm.create_lead(data, crm.bot(), source="prh")
                except Exception as exc:  # noqa: BLE001 - a duplicate created meanwhile
                    notes.append(f"{data['business_id']}: {exc}")
                    continue
                created.append(lead)
                taken += 1
            notes.append(f"PRH TOL {tol}{' ' + region if region else ''} page {page}/{-(-total // prh.PAGE_SIZE) or 1}: "
                         f"{len(companies)} records, {taken} new leads, skipped {skipped} ({res['url']})")
    return created


# ---------------------------------------------------------------- enrich (website role addresses)

def company_mailbox(addr: str | None) -> bool:
    """An address on a company-controlled mail domain (e.g. a marc+tag@ alias): a TEST lead, never an outside company."""
    return config.company_mail_domain(addr, sender())


async def enrich_lead(lead: dict, settings: dict) -> dict:
    if lead.get("test_mailbox"):
        # A TEST lead keeps its company-mailbox contact; a website is still checked but found addresses are only recorded.
        found = await web_contacts.find_role_addresses(lead["website"]) if lead.get("website") else {"checked": [], "emails": []}
        sets = {"enrichment": {"at": now_iso(), "checked": found["checked"], "emails": found["emails"], "test_mailbox": True},
                "lawful_basis": crm.LAWFUL_BASIS, "updated_at": now_iso(),
                "status": "contactable" if lead["status"] in ("new", "scored", "no_contact") else lead["status"]}
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": sets})
        await crm.add_activity("enriched", lead_id=lead["id"], actor=crm.bot(),
                               summary=f"TEST lead: company mailbox {lead.get('primary_email')} kept; website addresses recorded: "
                                       f"{len(found['emails'])}",
                               summary_fi=f"TESTI-liidi: yrityksen oma osoite {lead.get('primary_email')} säilyy; verkkosivun "
                                          f"osoitteita kirjattu: {len(found['emails'])}",
                               data={"checked": found["checked"], "emails": [e["email"] for e in found["emails"]]})
        return await score_lead({**lead, **sets}, settings)
    if not lead.get("website"):
        sets = {"status": "no_contact", "enrichment": {"at": now_iso(), "checked": ["no website in YTJ"], "emails": []}}
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {**sets, "updated_at": now_iso()}})
        return {**lead, **sets}
    found = await web_contacts.find_role_addresses(lead["website"])
    contacts = []
    for e in found["emails"]:
        if await crm.is_opted_out(e["email"]):
            continue
        contacts.append(await crm.upsert_contact(lead, e["email"], e["role"], e["source_url"]))
    best = next((c for c in contacts), None)
    sets = {"enrichment": {"at": now_iso(), "checked": found["checked"], "emails": found["emails"]},
            "contact_ids": [c["id"] for c in contacts], "updated_at": now_iso()}
    if best:
        sets.update(primary_email=best["email"], primary_role=best["role"], primary_contact_id=best["id"],
                    lawful_basis=crm.LAWFUL_BASIS, status="contactable" if lead["status"] in ("new", "scored", "no_contact") else lead["status"])
    else:
        sets["status"] = "no_contact" if lead["status"] in ("new", "scored") else lead["status"]
    await db.crm_leads.update_one({"id": lead["id"]}, {"$set": sets})
    await crm.add_activity("enriched", lead_id=lead["id"], actor=crm.bot(),
                           summary=f"Website checked: {len(contacts)} role address(es)" + (f", using {best['email']}" if best else ""),
                           summary_fi=f"Verkkosivu tarkistettu: {len(contacts)} rooliosoitetta" + (f", käytetään {best['email']}" if best else ""),
                           data={"checked": found["checked"], "emails": [c["email"] for c in contacts]})
    lead = {**lead, **sets}
    return await score_lead(lead, settings) if best else lead


# ---------------------------------------------------------------- compose (templates only, no model text)

def _identity_lines(fi: bool, settings: dict) -> list[str]:
    bid = settings.get("seller_business_id") or ""
    if fi:
        return [f"{config.COMPANY_NAME}, " + (f"Y-tunnus {bid}" if bid else "perustettava yhtiö (company in formation), Y-tunnus rekisteröinti kesken") +
                f", {config.COMPANY_ADDRESS}. {config.COMPANY_EMAIL}"]
    return [f"{config.COMPANY_NAME}, " + (f"business ID {bid}" if bid else "company in formation, business ID registration pending") +
            f", {config.COMPANY_ADDRESS}. {config.COMPANY_EMAIL}"]


def _lane_line(lead: dict, fi: bool) -> str | None:
    top = (lead.get("trade") or {}).get("top_lane")
    if not top:
        return None
    p = top["partner"]
    v = trade_stats.bn(top["value_eur"]).replace("€", "").strip()
    if fi:
        country = trade_stats.COUNTRY_FI.get(p, p)
        what = "tuonti maasta" if top["flow"] == "import" else "vienti maahan"
        return (f"Toimialanne tavaroissa (CN {top['cn']}) Suomen {what} {country} oli {v.replace('.', ',')} € vuonna "
                f"{top['period']} (Eurostat). Hoidamme juuri näitä kuljetuksia.")
    what = "imports from" if top["flow"] == "import" else "exports to"
    return f"In your goods (CN {top['cn']}) Finland's {what} {p} were EUR {v} in {top['period']} (Eurostat). That is the freight we move."


def compose(lead: dict, contact: dict, kind: str, settings: dict) -> tuple[str, list[str]]:
    fi = (lead.get("country") or "FI") == "FI"
    n = lead.get("follow_ups_sent", 0) + 1
    stop = crm.optout_url(contact["email"])
    lane = _lane_line(lead, fi)
    src = contact.get("source_url") or lead.get("website") or ""
    if fi:
        subject = {"first": f"Rahtihinnat heti verkossa – {lead['name']}",
                   "follow_up": f"Re: Rahtihinnat heti verkossa – {lead['name']}"}[kind]
        if kind == "first":
            lines = ["Hei,", "Huolitsija on digitaalinen huolintaliike: maantie-, meri-, lento- ja rautatiekuljetukset "
                     "sekä tullaus yhdestä paikasta, hinta heti verkossa ja seuranta koko matkan.",
                     *( [lane] if lane else []),
                     "Jos teillä on toistuvia kuljetuksia, vastatkaa tähän viestiin reitillä ja määrällä "
                     "(esim. \"Hampuri → Tampere, 2 rekkaa viikossa\"), niin lähetämme hinnan saman päivän aikana."]
        else:
            lines = ["Hei,", "palaan lyhyesti aiempaan viestiini." if n == 1 else "viimeinen muistutus, en kysy tämän jälkeen uudelleen.",
                     "Yksi reitti ja määrä riittää, niin lähetämme hinnan (esim. \"Shanghai → Helsinki, 1 kontti kuussa\")."]
        lines += ["—", *_identity_lines(True, settings),
                  f"Löysimme osoitteen {contact['email']} yrityksenne verkkosivulta ({src}). Tämä on yritysten välinen viesti "
                  f"yrityksen rooliosoitteeseen. Jos ette halua viestejämme, poistakaa osoite yhdellä klikkauksella: {stop}"]
    else:
        subject = {"first": f"Instant freight prices online – {lead['name']}",
                   "follow_up": f"Re: Instant freight prices online – {lead['name']}"}[kind]
        if kind == "first":
            lines = ["Hello,", "Huolitsija is a digital freight forwarder: road, sea, air and rail with customs clearance in "
                     "one place, an instant price online and tracking all the way.", *([lane] if lane else []),
                     "If you ship regularly, reply with a lane and volume (e.g. \"Hamburg → Tampere, 2 trucks a week\") "
                     "and we send a price the same day."]
        else:
            lines = ["Hello,", "a short follow-up to my earlier message." if n == 1 else "a last reminder; I will not ask again.",
                     "One lane and a volume is enough for a price (e.g. \"Shanghai → Helsinki, 1 container a month\")."]
        lines += ["—", *_identity_lines(False, settings),
                  f"We found {contact['email']} on your company website ({src}). This is a business-to-business message to a "
                  f"company role address. To stop our e-mails, one click: {stop}"]
    return subject, lines


# ---------------------------------------------------------------- delivery

def sandbox_address(addr: str) -> str | None:
    domain = config.sandbox_domain("CRM_SANDBOX_DOMAIN")
    if not domain:
        return None
    local, _, host = addr.lower().partition("@")
    return f"{re.sub(r'[^a-z0-9]+', '-', local).strip('-')}.{re.sub(r'[^a-z0-9]+', '-', host).strip('-')}@{domain}"


def provider() -> str:
    return (config.env("CRM_EMAIL_PROVIDER") or config.env("PARTNER_EMAIL_PROVIDER") or config.EMAIL_PROVIDER or "log").lower()


def sender() -> str:
    return config.env("CRM_EMAIL_FROM") or config.env("PARTNER_EMAIL_FROM") or config.EMAIL_FROM


def smtp_conf() -> dict:
    prefix = "CRM_SMTP" if config.env("CRM_SMTP_HOST") else "SMTP"
    return {"host": config.env(f"{prefix}_HOST"), "port": config.env(f"{prefix}_PORT"), "user": config.env(f"{prefix}_USER"),
            "password": config.env(f"{prefix}_PASSWORD"), "security": config.env(f"{prefix}_SECURITY", "starttls").lower()}


def _smtp_send(msg: EmailMessage, conf: dict):
    sec = conf["security"]
    port = int(conf["port"] or (465 if sec == "ssl" else 587 if sec == "starttls" else 25))
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(conf["host"], port, context=ctx, timeout=20) if sec == "ssl" else smtplib.SMTP(conf["host"], port, timeout=20)
    try:
        if sec == "starttls":
            smtp.starttls(context=ctx)
        if conf.get("user"):
            smtp.login(conf["user"], conf["password"])
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001
            pass


def _core_send_supports_headers() -> bool:
    from core.email import send_email
    return "headers" in inspect.signature(send_email).parameters


async def deliver(to: str, subject: str, html: str, text: str, headers: dict, message_id: str, ref: dict) -> dict:
    """SMTP (own sender, sets our Message-ID token) | core.email.send_email when it takes headers= (postmark/resend/smtp2go) | log."""
    prov, frm = provider(), sender()
    rec = {"id": new_id(), "to": to, "subject": subject, "tag": "crm_prospecting", "provider": prov, "ref": ref,
           "status": "queued", "created_at": now_iso(), "text": text[:20000], "from": frm, "message_id": message_id}
    conf = smtp_conf()
    from services.demo_world import boundary
    world = await boundary.capture_mail(to=to, subject=subject, text=text, html=html, from_addr=frm, headers=headers,
                                        message_id=message_id, channel="crm", ref=ref, tag="crm_prospecting") \
        if prov == "smtp" and conf["host"] else None
    if world:
        rec.update(status="sent", provider="world", world=world["world"])
        await db.email_outbox.insert_one(dict(rec))
        rec.pop("_id", None)
        return rec
    if prov == "smtp" and conf["host"]:
        # No folding: RFC 8058 needs List-Unsubscribe as a plain <url>; the default 78-char policy would RFC 2047-encode it.
        msg = EmailMessage(policy=email_policy.default.clone(max_line_length=998))
        msg["Message-ID"], msg["Date"] = message_id, formatdate(localtime=False)
        msg["From"], msg["To"], msg["Subject"] = frm, to, subject
        if config.env("CRM_REPLY_TO"):
            msg["Reply-To"] = config.env("CRM_REPLY_TO")
        for k, v in headers.items():
            msg[k] = v
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        try:
            await asyncio.get_running_loop().run_in_executor(None, _smtp_send, msg, conf)
            rec["status"] = "sent"
        except Exception as exc:  # noqa: BLE001
            log.warning("crm smtp to %s failed: %s", to, exc)
            rec.update(status="failed", error=str(exc)[:500])
        await db.email_outbox.insert_one(dict(rec))
        rec.pop("_id", None)
        return rec
    if prov in ("postmark", "resend", "smtp2go") and _core_send_supports_headers():
        from core.email import send_email
        out = await send_email(to, subject, html, text, tag="crm_prospecting", ref=ref, headers=headers,
                               from_addr=frm, reply_to=config.env("CRM_REPLY_TO") or None, provider=prov)
        return {**out, "message_id": out.get("message_id") or out.get("provider_id") or message_id}
    rec.update(status="logged", provider="log")
    await db.email_outbox.insert_one(dict(rec))
    rec.pop("_id", None)
    return rec


def _message_id(lead: dict) -> str:
    domain = (parseaddr(sender())[1].split("@") + ["huolitsija.fi"])[1]
    return f"<crm.{lead['ref']}.{secrets.token_hex(6)}@{domain}>"


# ---------------------------------------------------------------- the one gate for prospect e-mail

async def _primary_contact(lead: dict) -> dict | None:
    if lead.get("primary_contact_id"):
        c = await db.crm_contacts.find_one({"id": lead["primary_contact_id"], "status": "active"}, NO_ID)
        if c:
            return c
    return await db.crm_contacts.find_one({"lead_id": lead["id"], "status": "active"}, NO_ID)


async def _gate(lead: dict, kind: str, settings: dict, manual: bool) -> tuple[dict | None, str | None]:
    if not manual and await halted():
        return None, "Autopilot is halted (kill switch)"
    if lead["status"] in ("opted_out", "disqualified", "converted", "replied", "qualified", "nurture"):
        return None, f"lead status {lead['status']}"
    contact = await _primary_contact(lead)
    if not contact:
        return None, "no active role address"
    test_box = bool(lead.get("test_mailbox")) and company_mailbox(contact["email"])
    if not test_box and (not web_contacts.role_of(contact["email"]) or (lead.get("domain") and
                                                      web_contacts.base_domain(contact["email"].split("@")[1]) != lead["domain"])):
        return None, "not a role address on the company's own domain"
    if await crm.is_opted_out(contact["email"]):
        await db.crm_contacts.update_one({"id": contact["id"]}, {"$set": {"status": "opted_out"}})
        return None, "recipient opted out"
    if kind == "first":
        if lead.get("first_touch_at"):
            return None, "already contacted (one first touch per company)"
        if lead.get("domain") and await db.crm_leads.find_one({"domain": lead["domain"], "id": {"$ne": lead["id"]},
                                                               "first_touch_at": {"$ne": None}}, {"_id": 1}):
            return None, "another lead on the same company domain was already contacted"
    else:
        if not lead.get("first_touch_at"):
            return None, "no first touch yet"
        if lead.get("follow_ups_sent", 0) >= int(settings["crm_max_follow_ups"]):
            return None, f"max {settings['crm_max_follow_ups']} follow-ups sent"
        due = parse_iso(lead.get("last_touch_at")) + timedelta(days=float(settings["crm_follow_up_days"]))
        if due > now() and not manual:
            return None, f"follow-up not due before {due.isoformat()[:16]}"
    return contact, None


async def _agent_allows(lead: dict, contact: dict, kind: str, settings: dict) -> tuple[bool, list]:
    if decide is not None:
        d = await decide("sales", "prospect_email", subject=("crm_lead", lead["id"]), subject_label=lead["name"],
                         inputs={"score": lead.get("score"), "kind": kind, "to": contact["email"],
                                 "lawful_basis": crm.LAWFUL_BASIS["code"]},
                         decision={"send": kind}, confidence=None, switch="autopilot_enabled", create_approval=False,
                         title=f"Prospect e-mail to {lead['name']}", title_fi=f"Myyntiviesti: {lead['name']}")
        return bool(d.auto), list(d.reasons or [])
    if not settings.get("autopilot_enabled"):
        return False, [{"code": "autopilot_off", "fi": "Autopilotti on pois päältä", "en": "Autopilot is off"}]
    return True, []


async def send_prospect_email(lead: dict, kind: str = "first", *, actor: dict | None = None, manual: bool = False,
                              settings: dict | None = None) -> dict:
    """The only way a prospect e-mail leaves. -> {status: sent|logged|failed|blocked|held_limit|awaiting_person, reason?}"""
    settings = settings or await get_settings()
    lead = await crm.get_lead(lead["id"])
    contact, why = await _gate(lead, kind, settings, manual)
    if why:
        return {"status": "blocked", "reason": why}
    subject, lines = compose(lead, contact, kind, settings)
    if not manual:
        ok, reasons = await _agent_allows(lead, contact, kind, settings)
        if not ok:
            draft = {"kind": kind, "to": contact["email"], "subject": subject, "text": "\n\n".join(lines),
                     "reasons": reasons, "at": now_iso()}
            await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {
                "draft": draft, "status": "awaiting_person" if kind == "first" else lead["status"], "updated_at": now_iso()}})
            await _owner_card(lead, draft)
            return {"status": "awaiting_person", "reason": "; ".join(r.get("en", "") for r in reasons)}
    if kind == "first":
        claim = await db.crm_leads.update_one({"id": lead["id"], "first_touch_at": None}, {"$set": {"first_touch_at": now_iso()}})
        if claim.modified_count != 1:
            return {"status": "blocked", "reason": "already contacted (one first touch per company)"}
    if not await crm.reserve_send(settings["crm_daily_send_limit"]):
        if kind == "first":
            await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"first_touch_at": None}})
        return {"status": "held_limit", "reason": f"daily send limit {settings['crm_daily_send_limit']} reached"}
    real = contact["email"]
    to = sandbox_address(real) or real
    stop = crm.optout_url(real)
    message_id = _message_id(lead)
    headers = {"List-Unsubscribe": f"<{stop}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
               "X-Huolitsija-CRM": lead["ref"], "Precedence": "bulk", "Auto-Submitted": "no"}
    last = await db.crm_activities.find_one({"lead_id": lead["id"], "kind": "email_out", "status": {"$in": ["sent", "logged"]}},
                                            NO_ID, sort=[("at", -1)])
    if kind == "follow_up" and last and last.get("message_id"):
        headers.update({"In-Reply-To": last["message_id"], "References": last["message_id"]})
    if to != real:
        headers["X-Huolitsija-Original-To"] = real
        log.info("crm sandbox: %s -> %s (real address not mailed)", real, to)
    html, text = render(subject, lines, footer=f"{config.COMPANY_NAME} · {config.COMPANY_ADDRESS}")
    out = await deliver(to, subject, html, text, headers, message_id, {"type": "crm_lead", "id": lead["id"]})
    if out["status"] == "failed":
        await crm.release_send()
        if kind == "first":
            await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"first_touch_at": None}})
    act = await crm.add_activity("email_out", lead_id=lead["id"], opportunity_id=lead.get("opportunity_id"),
                                 actor=actor if manual else crm.bot(), direction="out",
                                 message_id=out.get("message_id") if out["status"] != "failed" else None,
                                 summary=f"{'First e-mail' if kind == 'first' else 'Follow-up'} to {real} ({out['status']})",
                                 summary_fi=f"{'Ensimmäinen viesti' if kind == 'first' else 'Muistutus'} osoitteeseen {real} ({out['status']})",
                                 status=out["status"], to=to, to_original=real, sandbox=to != real, subject=subject,
                                 email_id=out["id"], data={"kind": kind, "headers": headers, "provider": out.get("provider"),
                                                           "lawful_basis": crm.LAWFUL_BASIS["code"], "manual": manual})
    if out["status"] == "failed":
        return {"status": "failed", "reason": out.get("error"), "activity_id": act["id"]}
    days = float(settings["crm_follow_up_days"])
    sent_follow = lead.get("follow_ups_sent", 0) + (1 if kind == "follow_up" else 0)
    sets = {"status": "contacted", "last_touch_at": now_iso(), "draft": None, "updated_at": now_iso(),
            "follow_ups_sent": sent_follow, "last_message_id": out.get("message_id"),
            "next_follow_up_at": iso(now() + timedelta(days=days)) if sent_follow < int(settings["crm_max_follow_ups"]) else None}
    await db.crm_leads.update_one({"id": lead["id"]}, {"$set": sets, "$inc": {"touches": 1}})
    if not lead.get("opportunity_id"):
        opp = await crm.create_opportunity({"lead_id": lead["id"], "stage": "contacted"}, crm.bot())
        await db.crm_activities.update_one({"id": act["id"]}, {"$set": {"opportunity_id": opp["id"]}})
    else:
        opp = await crm.get_opportunity(lead["opportunity_id"])
        if opp["stage"] == "prospect":
            await crm.set_stage(opp["id"], "contacted", crm.bot(), reason="first e-mail sent")
    entry = {"kind": kind, "to": to, "to_original": real, "message_id": out.get("message_id"), "status": out["status"]}
    if manual:
        await audit(actor, "crm_prospect_email", "crm_lead", lead["id"], entry)
    else:
        await action("crm_prospect_email", "crm_lead", lead["id"], data=entry, reversible=False)
    await emit("crm.contacted", {"lead": {**lead, **sets}, "message": entry}, background=True)
    return {"status": out["status"], "to": to, "to_original": real, "message_id": out.get("message_id"), "activity_id": act["id"]}


async def draft_for(lead: dict, settings: dict, kind: str = "first", reason: str = "Autopilot is off") -> dict | None:
    contact = await _primary_contact(lead)
    if not contact:
        return None
    subject, lines = compose(lead, contact, kind, settings)
    draft = {"kind": kind, "to": contact["email"], "subject": subject, "text": "\n\n".join(lines), "reason": reason, "at": now_iso()}
    sets = {"draft": draft, "updated_at": now_iso()}
    if kind == "first":
        sets["status"] = "awaiting_person"
    await db.crm_leads.update_one({"id": lead["id"]}, {"$set": sets})
    await _owner_card(lead, draft)
    return draft


async def _owner_card(lead: dict, draft: dict) -> dict:
    """A parked draft is a card in the owner queue (one pending card per lead): approving it sends exactly this e-mail
    as that person; declining it parks the lead in nurture so the daily cycle does not draft it again."""
    first = draft["kind"] == "first"
    card = await request_approval(
        "prospect_email", "crm_lead", lead["id"],
        f"{'First e-mail' if first else 'Follow-up'} to {lead['name']} ({draft['to']})",
        summary=f"{draft['subject']}\n\n{draft['text']}"[:4000], reason=draft.get("reason") or "Autopilot is off",
        data={"kind": draft["kind"], "to": draft["to"], "subject": draft["subject"], "lead_ref": lead.get("ref")},
        link="/ops/commercial/pipeline",
        extra={"title_fi": f"{'Ensimmäinen viesti' if first else 'Muistutus'}: {lead['name']} ({draft['to']})"})
    await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"draft.approval_id": card["id"]}})
    return card


@on("approval.decided")
async def _on_prospect_card(p: dict):
    a, actor = p["approval"], p.get("actor") or {}
    if a.get("kind") != "prospect_email":
        return
    lead = await db.crm_leads.find_one({"id": a["ref_id"]}, NO_ID)
    if not lead:
        return
    if a.get("status") != "approved":
        await db.crm_leads.update_one({"id": lead["id"], "first_touch_at": None},
                                      {"$set": {"status": "nurture", "draft": None, "updated_at": now_iso()}})
        await audit(actor, "crm_prospect_email_declined", "crm_lead", lead["id"], {"approval_id": a["id"]})
        return
    res = await send_prospect_email(lead, (a.get("data") or {}).get("kind") or "first", actor=actor, manual=True)
    await db.approvals.update_one({"id": a["id"]}, {"$set": {"result": {k: res.get(k) for k in ("status", "reason", "message_id")}}})


# ---------------------------------------------------------------- one cycle

async def run_cycle(actor: dict | None = None, *, max_new: int | None = None, max_enrich: int | None = None) -> dict:
    started = now_iso()
    s = await get_settings()
    is_halted = await halted()
    summary = {"started_at": started, "actor": crm._actor(actor), "autopilot": bool(s["autopilot_enabled"]),
               "halted": is_halted, "found": 0, "scored": 0, "enriched": 0, "contactable": 0, "sent": 0, "drafts": 0,
               "held_limit": 0, "blocked": 0, "notes": [], "sends": []}
    if is_halted and not actor:
        summary["notes"].append("Autopilot halted: the scheduled cycle does nothing")
        return await _save_cycle(summary)
    notes = summary["notes"]
    new = await find_leads(s, max_new if max_new is not None else _int_env("CRM_FIND_PER_CYCLE", 20), notes)
    summary["found"] = len(new)
    for lead in await db.crm_leads.find({"status": "new"}, NO_ID).to_list(200):
        await score_lead(lead, s)
        summary["scored"] += 1
    todo = await db.crm_leads.find({"status": "scored", "enrichment": None}, NO_ID).sort("score", -1).to_list(
        max_enrich if max_enrich is not None else _int_env("CRM_ENRICH_PER_CYCLE", 10))
    for lead in todo:
        try:
            lead = await enrich_lead(lead, s)
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the cycle
            notes.append(f"enrich {lead['name']}: {type(exc).__name__}: {exc}")
            await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"enrichment": {"at": now_iso(), "error": str(exc)[:300]},
                                                                        "status": "no_contact"}})
            continue
        summary["enriched"] += 1
    ready = await db.crm_leads.find({"status": {"$in": ["contactable", "awaiting_person"]}, "first_touch_at": None},
                                    NO_ID).sort("score", -1).to_list(500)
    summary["contactable"] = len(ready)
    auto = bool(s["autopilot_enabled"]) and not is_halted
    for lead in ready:
        if not auto:
            if lead["status"] == "contactable" and await draft_for(lead, s, reason="Autopilot is off" if not is_halted else "Autopilot halted"):
                summary["drafts"] += 1
            continue
        res = await send_prospect_email(lead, "first", settings=s)
        summary["sends"].append({"lead_id": lead["id"], "name": lead["name"], **{k: res.get(k) for k in ("status", "to", "to_original", "reason")}})
        if res["status"] in ("sent", "logged"):
            summary["sent"] += 1
        elif res["status"] == "held_limit":
            summary["held_limit"] += 1
            break
        elif res["status"] == "awaiting_person":
            summary["drafts"] += 1
        else:
            summary["blocked"] += 1
    return await _save_cycle(summary)


async def _save_cycle(summary: dict) -> dict:
    summary["finished_at"] = now_iso()
    await db.system.update_one({"key": "crm:last_cycle"}, {"$set": {"key": "crm:last_cycle", "cycle": summary}}, upsert=True)
    await audit(summary.get("actor") if summary.get("actor", {}).get("type") == "user" else crm.bot(), "crm_prospect_cycle",
                "crm", "cycle", {k: v for k, v in summary.items() if k != "sends"})
    return summary


async def follow_ups(actor: dict | None = None) -> dict:
    s = await get_settings()
    out = {"at": now_iso(), "sent": 0, "drafts": 0, "held_limit": 0, "blocked": 0, "results": []}
    if await halted():
        out["note"] = "Autopilot halted"
        _state["last_follow_ups"] = out
        return out
    cutoff = iso(now() - timedelta(days=float(s["crm_follow_up_days"])))
    due = await db.crm_leads.find({"status": "contacted", "follow_ups_sent": {"$lt": int(s["crm_max_follow_ups"])},
                                   "last_touch_at": {"$lte": cutoff}}, NO_ID).sort("score", -1).to_list(200)
    for lead in due:
        if not s["autopilot_enabled"]:
            if not (lead.get("draft") or {}).get("kind") == "follow_up" and await draft_for(lead, s, "follow_up"):
                out["drafts"] += 1
            continue
        res = await send_prospect_email(lead, "follow_up", settings=s)
        out["results"].append({"lead_id": lead["id"], **{k: res.get(k) for k in ("status", "reason")}})
        key = {"sent": "sent", "logged": "sent", "held_limit": "held_limit"}.get(res["status"], "blocked")
        out[key] += 1
        if res["status"] == "held_limit":
            break
    _state["last_follow_ups"] = out
    return out


# ---------------------------------------------------------------- replies (IMAP)

def imap_configured() -> bool:
    return bool(config.env("CRM_IMAP_HOST") and config.env("CRM_IMAP_USER"))


def _decode(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value))) if value else ""
    except Exception:  # noqa: BLE001
        return value or ""


def _text(msg) -> str:
    plain = html_part = None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or str(part.get("Content-Disposition", "")).startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        t = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = t
        elif part.get_content_type() == "text/html" and html_part is None:
            html_part = t
    if plain is not None:
        return plain
    if html_part:
        return unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", html_part)))
    return ""


def parse_raw(raw: bytes) -> dict:
    m = email_lib.message_from_bytes(raw)
    return {"message_id": (m.get("Message-ID") or "").strip(), "from": _decode(m.get("From")), "to": _decode(m.get("To")),
            "subject": _decode(m.get("Subject")), "text": _text(m), "in_reply_to": (m.get("In-Reply-To") or "").strip(),
            "references": (m.get("References") or "").strip(), "auto_submitted": (m.get("Auto-Submitted") or "").lower(),
            "date": m.get("Date")}


def _imap() -> imaplib.IMAP4:
    host, sec = config.env("CRM_IMAP_HOST"), config.env("CRM_IMAP_SECURITY", "ssl").lower()
    port = int(config.env("CRM_IMAP_PORT") or (993 if sec == "ssl" else 143))
    if sec == "ssl":
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=20)
    else:
        conn = imaplib.IMAP4(host, port, timeout=20)
        if sec == "starttls":
            conn.starttls(ssl.create_default_context())
    conn.login(config.env("CRM_IMAP_USER"), config.env("CRM_IMAP_PASSWORD"))
    return conn


def _fetch_unseen_blocking() -> list[dict]:
    conn = _imap()
    try:
        conn.select(config.env("CRM_IMAP_FOLDER", "INBOX"))
        typ, data = conn.uid("search", None, "UNSEEN")
        out = []
        for uid in (data[0] or b"").split()[:50] if typ == "OK" else []:
            typ, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if typ == "OK" and raw:
                out.append({**parse_raw(raw), "imap_uid": uid.decode()})
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _mark_seen_blocking(uids: list[str]):
    conn = _imap()
    try:
        conn.select(config.env("CRM_IMAP_FOLDER", "INBOX"))
        for uid in uids:
            conn.uid("store", uid.encode(), "+FLAGS", "(\\Seen)")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _addr(v: str | None) -> str:
    return (parseaddr(v or "")[1] or "").lower()


async def match_lead(msg: dict) -> dict | None:
    ids = re.findall(r"<[^>]+>", f"{msg.get('in_reply_to', '')} {msg.get('references', '')}")
    if ids:
        # SMTP2GO replaces our Message-ID with <E{email_id}@message-id.smtpcorp.com>; the activity keeps the email_id.
        ids += [m.group(1) for i in ids if (m := re.fullmatch(r"<E([^@>]+)@message-id\.smtpcorp\.com>", i))]
        act = await db.crm_activities.find_one({"direction": "out", "message_id": {"$in": ids}}, NO_ID)
        if act and act.get("lead_id"):
            return await db.crm_leads.find_one({"id": act["lead_id"]}, NO_ID)
    for ref in TOKEN_RX.findall(" ".join(ids)):
        lead = await db.crm_leads.find_one({"ref": ref.upper()}, NO_ID)
        if lead:
            return lead
    sender_addr = _addr(msg.get("from"))
    if sender_addr:
        act = await db.crm_activities.find_one({"direction": "out", "$or": [{"to": sender_addr}, {"to_original": sender_addr}]},
                                               NO_ID, sort=[("at", -1)])
        if act:
            return await db.crm_leads.find_one({"id": act["lead_id"]}, NO_ID)
    return None


def _fresh_text(text: str) -> str:
    """The reply itself, without the quoted original."""
    out = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith(">") or re.match(r"\s*(On .+wrote:|.+kirjoitti:|-{2,}\s*Original Message|Lähettäjä:|From:)", line):
            break
        out.append(line)
    return "\n".join(out).strip()


KEYWORDS = [
    ("unsubscribe", r"\bunsubscribe\b|remove me|stop (sending|e-?mailing)|\bpoista(kaa)?\b|lopettak|älkää lähettäkö|ei enää viestejä|kieltäydyn"),
    ("out_of_office", r"out of (the )?office|automatic reply|auto-?reply|poissa toimistolta|automaattinen vastaus|lomalla|olen lomalla"),
    ("wrong_person", r"wrong person|not the right (person|contact)|väärä henkilö|en ole oikea|ei kuulu minulle|ohjaan viestin|please contact .+@"),
    ("not_interested", r"not interested|no thanks|no thank you|ei kiinnosta|emme ole kiinnostuneita|ei tarvetta|ei kiitos"),
    ("not_now", r"not now|maybe later|next year|later this year|ei nyt|myöhemmin|ensi vuonna|palataan (asiaan )?(myöhemmin|syksyllä|keväällä)"),
    ("interested", r"interested|send (us )?(a |your )?(quote|price|offer)|please quote|we ship|kiinnost|lähettäkää (hinta|tarjous)|"
                   r"tarjous|hinta|hintaa|kyllä|soittakaa|call me|\d+\s*(rekka|kontti|container|truck|lava|pallet)"),
]
PLACE = r"[A-ZÅÄÖ][\wåäöÅÄÖéü\-]+(?:\s[A-ZÅÄÖ][\wåäöÅÄÖéü\-]+)?"
LANE_RX = [re.compile(rf"\bfrom\s+({PLACE})\s+to\s+({PLACE})"),
           re.compile(rf"({PLACE})\s*(?:→|->|–|—|>|-)\s*({PLACE})")]
MODE_RX = [("sea", r"kontti|container|fcl|lcl|meri|sea|laiva|vessel"), ("air", r"lento|air|flight"),
           ("rail", r"juna|rail|rautatie"), ("road", r"rekka|truck|ftl|ltl|kuorma|trailer|maantie|road|lava|pallet")]
VOLUME_RX = re.compile(r"(\d+(?:[.,]\d+)?)\s*(rekkaa?|rekka|konttia?|containers?|trucks?|kuormaa?|lavaa?|pallets?|kg|tonnia|tons?|t)\b"
                       r"(?:\s*(?:/|per|a|in a|kuussa|viikossa|vuodessa|month|week|year|kk|vk)\s*\w*)?", re.I)


def parse_lane(text: str) -> dict | None:
    for rx in LANE_RX:
        m = rx.search(text)
        if m and m.group(1).lower() not in ("re", "fw", "fwd") and m.group(1) != m.group(2):
            low = text.lower()
            mode = next((md for md, pat in MODE_RX if re.search(pat, low)), "unknown")
            vol = VOLUME_RX.search(text)
            return {"origin": m.group(1)[:60], "destination": m.group(2)[:60], "mode": mode,
                    "volume": vol.group(0)[:120] if vol else None}
    return None


def keyword_intent(text: str) -> str:
    low = (text or "").lower()
    for intent, pat in KEYWORDS:
        if re.search(pat, low):
            return intent
    return "question"


async def classify(msg: dict) -> dict:
    fresh = _fresh_text(msg.get("text") or "") or (msg.get("text") or "")[:2000]
    if msg.get("auto_submitted", "no") not in ("", "no"):
        return {"intent": "out_of_office", "confidence": 1.0, "lane": None, "source": "header"}
    lane = parse_lane(fresh)
    from core.llm import llm_available, llm_json
    if llm_available():
        system = ("You classify a reply to a B2B freight forwarder's sales e-mail. Return JSON {\"intent\": one of "
                  + ", ".join(INTENTS) + ", \"confidence\": 0..1, \"lane\": null or {\"origin\": str, \"destination\": str, "
                  "\"mode\": one of road, sea, air, rail, unknown, \"volume\": str or null}}. Lane only if the sender names one.")
        try:
            got = await llm_json("crm_reply_classifier", system, f"Subject: {msg.get('subject')}\n\n{fresh[:4000]}", max_tokens=400)
            intent = got.get("intent") if isinstance(got, dict) else None
            if intent in INTENTS:
                ml = got.get("lane") if isinstance(got.get("lane"), dict) else None
                if ml and isinstance(ml.get("origin"), str) and isinstance(ml.get("destination"), str):
                    lane = {"origin": ml["origin"][:60], "destination": ml["destination"][:60],
                            "mode": ml.get("mode") if ml.get("mode") in MODES else "unknown",
                            "volume": str(ml["volume"])[:120] if ml.get("volume") else None}
                conf = got.get("confidence")
                return {"intent": intent, "confidence": float(conf) if isinstance(conf, (int, float)) else None,
                        "lane": lane, "source": "model"}
        except Exception as exc:  # noqa: BLE001 - fall back to keywords
            log.warning("crm reply classifier failed: %s", exc)
    return {"intent": keyword_intent(fresh), "confidence": None, "lane": lane, "source": "keywords"}


async def ingest_reply(msg: dict) -> dict:
    lead = await match_lead(msg)
    if not lead:
        return {"status": "unmatched", "message_id": msg.get("message_id")}
    if msg.get("message_id") and await db.crm_activities.find_one({"direction": "in", "message_id": msg["message_id"]}, {"_id": 1}):
        return {"status": "duplicate", "lead_id": lead["id"]}
    cls = await classify(msg)
    intent = cls["intent"]
    try:
        act = await crm.add_activity("email_in", lead_id=lead["id"], opportunity_id=lead.get("opportunity_id"),
                                     actor={"type": "system", "id": "prospect", "name": _addr(msg.get("from"))}, direction="in",
                                     message_id=msg.get("message_id") or None, summary=f"Reply ({intent}): {msg.get('subject')}",
                                     summary_fi=f"Vastaus ({intent}): {msg.get('subject')}", subject=msg.get("subject"),
                                     data={"from": msg.get("from"), "text": (msg.get("text") or "")[:8000], "intent": intent,
                                           "classifier": cls})
    except DuplicateKeyError:
        return {"status": "duplicate", "lead_id": lead["id"]}
    await act_on_reply(lead, msg, cls)
    await emit("crm.reply", {"lead": lead, "message": {k: msg.get(k) for k in ("message_id", "from", "subject")}, "intent": intent},
               background=True)
    return {"status": "ingested", "lead_id": lead["id"], "intent": intent, "activity_id": act["id"], "lane": cls.get("lane")}


async def try_ingest_reply(raw: bytes) -> dict | None:
    """Shared inbound hook: a reply (In-Reply-To/References) to a prospect e-mail is ingested; None for any other mail,
    which then goes on to customer intake. A lead already converted to a customer is left to the customer channel."""
    msg = parse_raw(raw)
    if not (msg.get("in_reply_to") or msg.get("references")):
        return None
    lead = await match_lead(msg)
    if not lead or lead.get("status") == "converted":
        return None
    return await ingest_reply(msg)


async def _contact_for(lead: dict, msg: dict) -> dict | None:
    sender_addr = _addr(msg.get("from"))
    out = await db.crm_activities.find_one({"lead_id": lead["id"], "direction": "out", "$or": [{"to": sender_addr},
                                           {"to_original": sender_addr}]}, NO_ID, sort=[("at", -1)])
    real = (out or {}).get("to_original") or lead.get("primary_email")
    return await db.crm_contacts.find_one({"lead_id": lead["id"], "email": real}, NO_ID) if real else None


async def act_on_reply(lead: dict, msg: dict, cls: dict):
    intent, bot = cls["intent"], crm.bot()
    s = await get_settings()
    contact = await _contact_for(lead, msg)
    opp_id = lead.get("opportunity_id")
    if not opp_id and intent not in ("out_of_office",):
        opp_id = (await crm.create_opportunity({"lead_id": lead["id"], "stage": "contacted"}, bot))["id"]
    stop_seq = {"next_follow_up_at": None, "updated_at": now_iso()}
    if intent == "unsubscribe":
        if contact:
            await crm.add_optout(contact["email"], source=f"reply {msg.get('message_id')}")
        if opp_id:
            await crm.set_stage(opp_id, "lost", bot, reason="unsubscribed")
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {**stop_seq, "status": "opted_out"}})
    elif intent == "not_interested":
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {**stop_seq, "status": "disqualified"}})
        await crm.set_stage(opp_id, "lost", bot, reason="not interested")
    elif intent == "not_now":
        await crm.set_stage(opp_id, "replied", bot, reason="not now")
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"status": "nurture", "updated_at": now_iso(),
                                                                    "next_follow_up_at": iso(now() + timedelta(days=90))}})
    elif intent == "out_of_office":
        nxt = iso(now() + timedelta(days=float(s["crm_follow_up_days"])))
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"next_follow_up_at": nxt, "last_touch_at": now_iso()}})
    elif intent == "wrong_person":
        if contact:
            await db.crm_contacts.update_one({"id": contact["id"]}, {"$set": {"status": "wrong_person"}})
        for e in web_contacts.extract(msg.get("text") or "", lead.get("domain") or ""):
            if contact is None or e["email"] != contact["email"]:
                c = await crm.upsert_contact(lead, e["email"], e["role"], f"reply {msg.get('message_id')}")
                await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"primary_contact_id": c["id"], "primary_email": c["email"]}})
                break
        await crm.set_stage(opp_id, "replied", bot, reason="wrong person")
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {"status": "replied", "next_follow_up_at": now_iso()}})
    else:  # interested, question
        stage = "qualified" if intent == "interested" else "replied"
        await crm.set_stage(opp_id, stage, bot, reason=f"reply: {intent}")
        await db.crm_leads.update_one({"id": lead["id"]}, {"$set": {
            "status": stage, "updated_at": now_iso(), "next_follow_up_at": iso(now() + timedelta(hours=4))}})
        await db.crm_opportunities.update_one({"id": opp_id}, {"$set": {"next_follow_up_at": iso(now() + timedelta(hours=4))}})
        if cls.get("lane"):
            ln = cls["lane"]
            await crm.add_note(opp_id, f"Lane request for quoting: {ln['origin']} → {ln['destination']} ({ln['mode']})"
                               + (f", {ln['volume']}" if ln.get("volume") else ""), bot, kind="lane_request",
                               data={"lane": ln, "message_id": msg.get("message_id")})
            await db.crm_opportunities.update_one({"id": opp_id}, {"$addToSet": {"lanes": ln}})
        if intent == "interested" and await autopilot_on(s):
            allowed = True
            if decide is not None:
                d = await decide("sales", "convert_lead", subject=("crm_lead", lead["id"]), subject_label=lead["name"],
                                 inputs={"intent": intent}, decision={"convert": True}, confidence=cls.get("confidence"),
                                 switch="autopilot_enabled", create_approval=False)
                allowed = bool(d.auto)
            if allowed:
                await crm.convert_lead(lead["id"], bot)
                await action("crm_convert_lead", "crm_lead", lead["id"], data={"intent": intent}, reversible=False)


async def poll_replies() -> dict:
    if not imap_configured():
        return {"configured": False}
    try:
        msgs = await asyncio.get_running_loop().run_in_executor(None, _fetch_unseen_blocking)
    except Exception as exc:  # noqa: BLE001
        _state.update(last_poll=now_iso(), last_poll_error=f"{type(exc).__name__}: {exc}")
        return {"configured": True, "error": _state["last_poll_error"]}
    results, seen = [], []
    for m in msgs:
        try:
            results.append(await ingest_reply(m))
            seen.append(m["imap_uid"])
        except Exception as exc:  # noqa: BLE001 - left unseen, retried next poll
            log.exception("crm reply %s failed", m.get("message_id"))
            results.append({"status": "error", "error": str(exc)[:300]})
    if seen:
        await asyncio.get_running_loop().run_in_executor(None, _mark_seen_blocking, seen)
    _state.update(last_poll=now_iso(), last_poll_error=None)
    return {"configured": True, "fetched": len(msgs), "results": results}


# ---------------------------------------------------------------- status

async def status() -> dict:
    s = await get_settings()
    last = (await db.system.find_one({"key": "crm:last_cycle"}, NO_ID) or {}).get("cycle")
    from core.llm import llm_available
    return {
        "autopilot_enabled": bool(s["autopilot_enabled"]), "halted": await halted(),
        "sending": bool(s["autopilot_enabled"]) and not await halted(),
        "sends_today": await crm.sends_today(), "daily_send_limit": s["crm_daily_send_limit"],
        "follow_up_days": s["crm_follow_up_days"], "max_follow_ups": s["crm_max_follow_ups"],
        "target_industries": s["crm_target_industries"], "target_regions": s["crm_target_regions"],
        "email": {"provider": provider(), "from": sender(), "smtp_host_set": bool(smtp_conf()["host"]),
                  "sandbox_domain": config.sandbox_domain("CRM_SANDBOX_DOMAIN") or None,
                  "live": not config.sandbox_domain("CRM_SANDBOX_DOMAIN") and provider() != "log"},
        "imap": {"configured": imap_configured(), "last_poll": _state["last_poll"], "error": _state["last_poll_error"]},
        "trade_stats_source": trade_stats.source(), "classifier": "model" if llm_available() else "keywords",
        "runtime": {"decide": decide is not None, "jobs": _job is not None},
        "schedules": {"crm.prospect_cycle": f"daily {config.env('CRM_PROSPECT_DAILY_AT') or '08:30'} Europe/Helsinki",
                      "crm.follow_ups": "hourly", "crm.poll_replies": f"every {_int_env('CRM_POLL_SECONDS', 60)} s"},
        "last_cycle": last, "last_follow_ups": _state["last_follow_ups"],
        "counts": {"leads": await db.crm_leads.count_documents({}), "contacts": await db.crm_contacts.count_documents({}),
                   "opportunities": await db.crm_opportunities.count_documents({}),
                   "optouts": await db.crm_optouts.count_documents({})},
    }


# ---------------------------------------------------------------- schedule

def _daily_due(at: datetime | None = None) -> bool:
    hhmm = config.env("CRM_PROSPECT_DAILY_AT") or "08:30"
    if hhmm.lower() == "off":
        return False
    local = (at or now()).astimezone(crm.HEL)
    h, m = (int(x) for x in hhmm.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return start <= local < start + timedelta(hours=3)


if _job is not None and _schedule is not None:  # s2 durable jobs
    @_job("crm.prospect_cycle", agent=True, timeout=1800, label_fi="Asiakashaku", label_en="Prospecting cycle")
    async def _job_cycle(j: dict):
        return {k: v for k, v in (await run_cycle(None)).items() if k != "sends"}

    @_job("crm.follow_ups", agent=True, timeout=900, label_fi="Myynnin muistutukset", label_en="Sales follow-ups")
    async def _job_follow_ups(j: dict):
        return await follow_ups()

    @_job("crm.poll_replies", timeout=120, label_fi="Myynnin vastaukset", label_en="Sales replies")
    async def _job_poll(j: dict):
        return await poll_replies()

    _schedule("crm.prospect_daily", job="crm.prospect_cycle", daily_at=config.env("CRM_PROSPECT_DAILY_AT") or "08:30",
              label_fi="Asiakashaku päivittäin", label_en="Daily prospecting")()
    _schedule("crm.follow_ups", job="crm.follow_ups", every_seconds=3600,
              label_fi="Myynnin muistutukset tunnin välein", label_en="Sales follow-ups every hour")()
    _schedule("crm.poll_replies", job="crm.poll_replies", every_seconds=_int_env("CRM_POLL_SECONDS", 60),
              label_fi="Myynnin vastausten haku", label_en="Fetch sales replies")()
else:  # core.lifecycle fallback
    @background_loop(seconds=300)
    async def crm_prospect_daily():
        if not _daily_due():
            return
        day = crm.helsinki_day()
        res = await db.system.update_one({"key": f"crm:daily:{day}"}, {"$setOnInsert": {"key": f"crm:daily:{day}", "at": now_iso()}},
                                         upsert=True)
        if res.upserted_id is None:
            return
        if await halted():
            return
        await run_cycle(None)

    @background_loop(seconds=3600)
    async def crm_follow_ups():
        await follow_ups()

    @background_loop(seconds=_int_env("CRM_POLL_SECONDS", 60))
    async def crm_poll_replies():
        if imap_configured():
            await poll_replies()
