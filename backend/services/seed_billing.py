"""V4 demo data (startup order 50, SEED_DEMO only, idempotent via db.system "seed:v4:v1").

Built through the real services on V1's demo shipments, so every number is computed the same way as for a live
customer: invoices from the accepted quotes (some paid by bank transfer, some open, one overdue), message threads
with customer questions and ops answers that quote the shipment's own facts, exceptions from the detection rules
plus the carrier-reported ones on exception / customs-hold shipments, and one damage claim waiting for a decision.
Everything carries demo: true. No e-mail is sent for demo records.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from core import config
from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, iso, now, now_iso, parse_iso

log = logging.getLogger("seed_billing")
MARKER = "seed:v4:v1"


def _ago(**kw) -> str:
    return iso(now() - timedelta(**kw))


async def _ops_author() -> dict:
    u = await db.users.find_one({"email": config.DEMO_OPS_EMAIL}, {"_id": 0, "id": 1, "name": 1})
    return {"type": "ops", "id": (u or {}).get("id"), "name": (u or {}).get("name") or "Huolitsija operations",
            "role": "ops_admin"}


async def _customer_author(company_id: str, fallback_name: str) -> dict:
    u = await db.users.find_one({"company_id": company_id, "disabled": {"$ne": True}}, {"_id": 0, "id": 1, "name": 1})
    return {"type": "customer", "id": (u or {}).get("id"), "name": (u or {}).get("name") or fallback_name}


def _fi_date(iso_s: str | None) -> str:
    d = parse_iso(iso_s) if iso_s else None
    return f"{d.day}.{d.month}.{d.year}" if d else "—"


EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
      "PL", "PT", "RO", "SK", "SI", "ES", "SE"}


async def _link_quote(s: dict) -> str | None:
    """V1's demo shipments are inserted directly, without the quote they would have been booked from. Price each
    one through V2's real pricing engine (same rates, margins and VAT codes a live quote gets), store that as the
    booked demo quote and link it, so the invoice comes from the accepted quote exactly as for a live booking."""
    if s.get("quote_id"):
        return s["quote_id"]
    from core.refs import next_ref
    from core.settings import get_settings
    from services import pricing

    company = await db.companies.find_one({"id": s.get("company_id")}, NO_ID)
    if not company:
        return None
    o, d = s.get("origin") or {}, s.get("destination") or {}
    loc = lambda x: {k: x.get(k) for k in ("name", "unlocode", "lat", "lon", "country", "address") if x.get(k) is not None}
    cargo = dict(s.get("cargo") or {})
    iso_alias = {"20DV": "20GP", "40DV": "40GP", "40HQ": "40HC", "45HQ": "45HC", "20RE": "20RF", "40RH": "40RF"}
    if cargo.get("containers"):
        cargo["containers"] = [{**c, "type": iso_alias.get(str(c.get("type")).upper(), c.get("type"))} for c in cargo["containers"]]
    body = {"mode": s.get("mode") if s.get("mode") in ("sea", "road", "air", "rail") else None,
            "origin": loc(o), "destination": loc(d), "incoterm": s.get("incoterm"), "incoterm_place": s.get("incoterm_place"),
            "cargo": cargo, "customs_clearance": bool(o.get("country") not in EU and d.get("country") in EU),
            "insurance": {"cover": "NONE"}}
    try:
        priced = await pricing.quote_pricing(body, company=company, strict=False)
    except Exception as exc:  # noqa: BLE001 - a lane the engine cannot price stays a hand-made draft
        log.info("seed quote for %s not priced: %s", s.get("reference"), exc)
        return None
    options = priced.get("options") or []
    legs = s.get("legs") or []
    # main carriage: the shipment's mode, or for multimodal the longest-haul mode among its legs (sea > rail > air > road)
    main = s.get("mode") if s.get("mode") in ("sea", "road", "air", "rail") else next(
        (m for m in ("sea", "rail", "air", "road") if any(lg.get("mode") == m for lg in legs)), None)
    carrier = next((lg.get("carrier") for lg in legs if lg.get("mode") == main and lg.get("carrier")), None)
    pick = (next((x for x in options if x.get("mode") == main and carrier and x.get("carrier_name") == carrier), None)
            or next((x for x in options if x.get("mode") == main), None))
    if not pick:
        log.info("seed quote for %s: no %s option on this lane (stays a draft for ops)", s.get("reference"), main)
        return None
    settings = await get_settings()
    try:
        from routes import quotes as v2

        q = v2._base_doc(priced, source="ops", settings=settings)  # V2's own document shape
    except Exception:  # noqa: BLE001
        q = {"id": __import__("uuid").uuid4().hex, "source": "ops", "request": priced["request"], "options": options,
             "not_quoted": priced.get("not_quoted"), "warnings": priced.get("warnings"), "valid_until": priced.get("valid_until"),
             "terms_version": settings.get("terms_version"), "approval": {"required": False}}
    booked_at = s.get("created_at") or now_iso()
    q.update(number=await next_ref("Q"), company_id=company["id"], customer_name=company.get("name"),
             requested_by={"type": "customer", "name": company.get("name")}, status="booked", accepted_option_id=pick["id"],
             shipment_id=s["id"], shipment_reference=s.get("reference"), created_at=booked_at, updated_at=booked_at,
             priced_at=booked_at, demo=True, published=True)
    await db.quotes.insert_one(dict(q))
    price = {"total_cents": pick.get("total_cents"), "currency": "EUR", "quote_id": q["id"], "quote_number": q["number"]}
    await db.shipments.update_one({"id": s["id"]}, {"$set": {"quote_id": q["id"], "option_id": pick["id"], "price": price}})
    s.update(quote_id=q["id"], option_id=pick["id"], price=price)
    return q["id"]


# Earlier domestic work for regular customers (before the demo shipments), so receivables have history:
# (company, days ago issued, days after issue paid or None, lines (description, qty, unit, unit price cents)).
HISTORY = [
    ("Arktinen Elektroniikka Oy", 41, 12, [("Kotimaan jakelukuljetus Vantaa → Oulu, 8 lavaa", 1, "kpl", 68_000),
                                           ("Takaluukkunostin", 1, "kpl", 4_500)]),
    ("Pohjolan Kaluste Oy", 37, 9, [("Kotimaan jakelukuljetus Lahti → Helsinki, 6 lavaa", 1, "kpl", 29_000),
                                    ("Kuljetusvakuutuksen käsittely", 1, "kpl", 2_500)]),
    ("Kymen Teräskauppa Oy", 31, None, [("Kotimaan kuljetus Kotka Mussalo → Kouvola, 2 × 20' kontti", 2, "kontti", 41_000),
                                        ("Kontin odotus purussa", 3, "h", 7_500)]),
    ("Pohjolan Kaluste Oy", 24, None, [("Kotimaan jakelukuljetus Helsinki → Espoo, 4 lavaa", 1, "kpl", 18_500),
                                       ("Kantoapu (2 hlö)", 1.5, "h", 6_800)]),
]


async def _seed_history() -> int:
    from services import billing

    made = 0
    for name, days_ago, paid_after, lines in HISTORY:
        company = await db.companies.find_one({"name": name, "demo": True}, {"_id": 0, "id": 1})
        if not company:
            continue
        issue = billing.today_fi() - timedelta(days=days_ago)
        inv = await billing.create_manual_invoice({
            "company_id": company["id"], "supply_date": (issue - timedelta(days=1)).isoformat(),
            "lines": [{"description": d, "qty": q, "unit": u, "unit_price_cents": p, "vat_code": "FI_25_5"} for d, q, u, p in lines],
        }, None)
        await db.invoices.update_one({"id": inv["id"]}, {"$set": {"demo": True}})
        inv = await billing.issue_invoice(inv["id"], None, send=False, issue_date=issue)
        await db.invoices.update_one({"id": inv["id"]}, {"$set": {"issued_at": f"{issue.isoformat()}T07:30:00Z"}})
        if paid_after is not None:
            pay_day = issue + timedelta(days=paid_after)
            await billing.record_payment(inv["id"], amount_cents=inv["total_cents"], method="bank_transfer",
                                         ref=f"ARK-{pay_day.strftime('%Y%m%d')}-{inv['reference'].replace(' ', '')[-6:]}",
                                         paid_at=pay_day.isoformat(),
                                         details={"note": "Demo: bank transfer matched by reference"})
        made += 1
    return made


async def _seed_invoices(delivered: list[dict]) -> int:
    from services import billing

    token = billing.QUIET_EMAIL.set(True)
    made = 0
    try:
        made += await _seed_history()
        for i, s in enumerate(delivered[:8]):
            await _link_quote(s)
            try:
                inv = await billing.create_invoice_for_shipment(s["id"], None)
            except billing.BillingError as exc:
                log.info("seed invoice for %s skipped: %s", s.get("reference"), exc)
                continue
            made += 1
            await db.invoices.update_one({"id": inv["id"]}, {"$set": {"demo": True}})
            if inv["status"] == "draft":
                continue
            delivered_on = billing.to_date(s.get("delivered_at")) or billing.today_fi()
            issue = delivered_on + timedelta(days=1)
            if issue > billing.today_fi():
                issue = billing.today_fi()
            due = issue + timedelta(days=int(inv.get("payment_terms_days") or 14))
            status = "open"
            await db.invoices.update_one({"id": inv["id"]}, {"$set": {"issue_date": issue.isoformat(), "due_date": due.isoformat(),
                                                                      "status": status}})
            inv = await db.invoices.find_one({"id": inv["id"]}, NO_ID)
            await billing._store_pdf(inv)
            pay_day = issue + timedelta(days=4)
            if i % 3 == 2 and pay_day <= billing.today_fi():
                await billing.record_payment(inv["id"], amount_cents=inv["total_cents"], method="bank_transfer",
                                             ref=f"ARK-{pay_day.strftime('%Y%m%d')}-{inv['reference'].replace(' ', '')[-6:]}",
                                             paid_at=pay_day.isoformat(),
                                             details={"note": "Demo: bank transfer matched by reference"})
        await billing.mark_overdue()
    finally:
        billing.QUIET_EMAIL.reset(token)
    return made


def _hold_fi(reason: str | None) -> tuple[str, str]:
    """V1's demo hold reasons are English; the Finnish sentence (why, what we need) for messages and titles."""
    r = (reason or "").lower()
    if "packing list" in r:
        return ("Tulli pyytää pakkausluetteloa, koska kauppalaskun ja rahtikirjan kollimäärät eroavat.",
                "Jos teillä on toimittajan pakkausluettelo, liittäkää se tähän viestiin, niin toimitamme sen Tullille.")
    if "inspection" in r:
        return ("Tulli tekee lähetykselle fyysisen tarkastuksen.",
                "Teiltä ei tarvita tällä hetkellä mitään; ilmoitamme heti, kun tarkastus on tehty.")
    if "authorisation" in r or "end-use" in r:
        return ("Tulli pyytää käyttötarkoituslupaa ennen tavaran luovutusta.",
                "Jos teillä on voimassa oleva lupapäätös, liittäkää se tähän viestiin.")
    return "Tulli on pyytänyt lisäselvitystä.", "Kerromme heti, jos tarvitsemme teiltä asiakirjoja."


async def _seed_threads(active: list[dict], delivered: list[dict]) -> tuple[int, str | None]:
    from services import messaging

    ops = await _ops_author()
    made = 0
    ai_thread = None
    # The demo customer login's own shipments first, so the portal shows real conversations too.
    demo_user = await db.users.find_one({"email": "customer.demo@huolitsija.fi"}, {"_id": 0, "company_id": 1})  # seed_core
    mine = (demo_user or {}).get("company_id")
    active = sorted(active, key=lambda x: x.get("company_id") != mine)
    delivered = sorted(delivered, key=lambda x: x.get("company_id") != mine)
    by_status = {}
    for s in active:
        by_status.setdefault(s["status"], []).append(s)
    transit = (by_status.get("in_transit") or by_status.get("at_hub") or [None])[0]
    hold = (by_status.get("customs_hold") or [None])[0]
    done = delivered[0] if delivered else None

    async def thread(s: dict, convo: list[tuple[str, str, int]]) -> dict:
        nonlocal made
        cust = await _customer_author(s["company_id"], "Asiakas")
        t = await messaging.get_or_create_thread(company_id=s["company_id"], shipment_id=s["id"], subject=s.get("reference"),
                                                 created_by=cust, demo=True, new=True)
        for who, body, hours_ago in convo:
            await messaging.add_message(t, body=body, author=cust if who == "c" else ops, created_at=_ago(hours=hours_ago),
                                        notify=False, demo=True)
        made += 1
        return t

    if transit:
        leg = next((lg for lg in transit.get("legs") or [] if lg.get("mode") == transit.get("mode")), {})
        vessel = (leg.get("vessel") or {}).get("name")
        eta = _fi_date(transit.get("eta"))
        dest = (transit.get("destination") or {}).get("name") or "määränpäähän"
        t = await thread(transit, [
            ("c", f"Hei, onko {transit['reference']} vielä aikataulussa? Asiakkaamme kysyy toimituspäivää.", 30),
            ("o", f"Hei! {transit['reference']} on matkalla" + (f" aluksella {vessel}" if vessel else "") +
             f", arvioitu saapuminen {eta}. Ilmoitamme heti, jos aikataulu muuttuu.", 28),
            ("c", f"Kiitos. Ehtiikö toimitus {dest} vielä samalla viikolla, ja tarvitsetteko meiltä vielä jotain asiakirjoja?", 2),
        ])
        ai_thread = t["id"]
    if hold:
        reason = next((h.get("reason") for h in hold.get("holds") or [] if h.get("reason")), None)
        why, ask = _hold_fi(reason)
        await thread(hold, [
            ("c", f"Miksi {hold['reference']} on pysähtynyt tulliin? Tarvitaanko meiltä jotain?", 20),
            ("o", f"Hei! {why} Hoidamme asian ja palaamme tänään. {ask}", 18),
        ])
    if done:
        # A question about a real line on that shipment's invoice, answered with what the line is.
        explain = {
            "ETS": "EU:n päästökauppalisä: varustamot ostavat päästöoikeudet aluksen päästöille EU:n päästökauppajärjestelmässä "
                   "ja veloittavat ne kontti- tai tonnikohtaisena lisänä.",
            "FUELEU": "FuelEU Maritime -lisä: vuoden 2025 alusta varustamojen on vähennettävä polttoaineen kasvihuonekaasu"
                      "intensiteettiä, ja kustannus veloitetaan lisänä.",
            "DTHC": "määräsataman terminaalimaksu: terminaali veloittaa kontin purkamisesta aluksesta ja käsittelystä satamassa.",
            "BAF": "polttoainelisä, joka seuraa varustamon polttoainekustannusta ja päivitetään yleensä kuukausittain.",
            "ROAD_FUEL": "maantiekuljetuksen polttoainelisä, joka seuraa dieselin hintaa.",
        }
        inv = await db.invoices.find_one({"shipment_id": done["id"], "kind": "invoice", "number": {"$ne": None}}, NO_ID)
        line = next((ln for code in explain for ln in (inv or {}).get("lines") or [] if ln.get("code") == code), None)
        if inv and line:
            await thread(done, [
                ("c", f"Laskulla {inv['number']} on rivi \"{line['description']}\". Mitä se tarkoittaa?", 50),
                ("o", f"Hei! \"{line['description']}\" on {explain[line['code']]} Se oli samana rivinä hyväksytyssä "
                      f"tarjouksessanne. Kysykää vain, jos jokin muu rivi askarruttaa.", 47),
            ])
    return made, ai_thread


async def _seed_exceptions(active: list[dict]) -> int:
    """Carrier- and ops-reported exceptions first, then one pass of the detection rules (which folds its own hits into
    those where they describe the same incident). AI assessments: two real ones, the rest stay "not assessed yet"."""
    from services import exceptions_agent as xa

    n = 0
    first = True
    for s in active:
        if s["status"] == "exception":
            note = s.get("exception_note") or "Carrier reported a problem with this shipment"
            _, created = await xa.create_exception(
                shipment_id=s["id"], type="damage" if "damage" in note.lower() or "vauri" in note.lower() else "delay",
                severity="high", title=note[:120], description=f"{s.get('reference')}: {note}", detected_by="carrier",
                demo=True, assess=first, rule_key=f"seed-carrier:{s['id']}")
            n += created
            first = False
        elif s["status"] == "customs_hold":
            reason = next((h.get("reason") for h in s.get("holds") or [] if h.get("reason")), "Customs hold")
            _, created = await xa.create_exception(
                shipment_id=s["id"], type="customs_hold", severity="medium", title=f"Customs hold: {reason}"[:120],
                title_i18n={"en": f"Customs hold: {reason}"[:120], "fi": ("Tullipysäytys: " + _hold_fi(reason)[0].rstrip("."))[:120]},
                description_i18n={"en": f"{s.get('reference')} held by Finnish Customs. {reason}",
                                  "fi": f"{s.get('reference')} on Tullin pysäyttämänä. {_hold_fi(reason)[0]}"},
                description=f"{s.get('reference')} held by Finnish Customs. {reason}", detected_by="ops", demo=True,
                assess=False, rule_key=f"seed-hold:{s['id']}")
            n += created
    try:
        res = await xa.run_detection(assess=False)
        n += res.get("created", 0)
        docs = await db.exceptions.find_one({"type": "missing_docs", "detected_by": "rule", "status": "open"},
                                            {"_id": 0, "id": 1}, sort=[("severity", 1)])
        if docs:
            from services.comms_agent import spawn

            spawn(xa.assess_exception(docs["id"]))
    except Exception:  # noqa: BLE001
        log.exception("seed detection failed")
    return n


async def _seed_claim(delivered: list[dict]) -> int:
    from services import claims

    demo_user = await db.users.find_one({"email": "customer.demo@huolitsija.fi"}, {"_id": 0, "company_id": 1})
    mine = (demo_user or {}).get("company_id")
    for s in sorted(delivered, key=lambda x: x.get("company_id") != mine):  # the demo login sees it in the portal
        if (s.get("cargo") or {}).get("weight_kg") and s.get("company_id"):
            if await db.claims.find_one({"shipment_id": s["id"]}, {"_id": 1}):
                return 0
            cust = await _customer_author(s["company_id"], "Asiakas")
            kg = round(float(s["cargo"]["weight_kg"]) * 0.08, 1)
            packages = max(1, int((s.get("cargo") or {}).get("packages") or 10) // 10)
            actor = {"id": cust["id"], "name": cust["name"], "org_type": "customer"}
            cl = await claims.create_claim({
                "shipment_id": s["id"], "type": "damage", "amount_claimed_cents": 186_000, "gross_kg_affected": kg,
                "packages_affected": packages,
                "description": (f"{packages} kollia vaurioitui kuljetuksessa: pahvilaatikot litistyneet ja sisältö rikki. "
                                "Vauriot merkittiin rahtikirjaan vastaanotossa, kuvat liitteenä."),
                "date_of_loss": (s.get("delivered_at") or now_iso())[:10],
                "notice_given_on": (s.get("delivered_at") or now_iso())[:10]}, actor, company_id=s["company_id"])
            await db.claims.update_one({"id": cl["id"]}, {"$set": {"demo": True}})
            await claims.submit_claim(cl["id"], actor)
            ops = await _ops_author()
            ops_actor = {"id": ops["id"], "name": ops["name"], "org_type": "internal"}
            await claims.start_review(cl["id"], ops_actor)
            cap = (await db.claims.find_one({"id": cl["id"]}, NO_ID)).get("cap_eur_cents")
            amount = min(186_000, cap) if cap else 120_000
            await claims.propose_decision(cl["id"], decision="approve", amount_cents=amount,
                                          note="Vaurio todettu vastaanotossa; korvaus enintään vastuunrajoituksen mukaan.",
                                          actor=ops_actor)
            return 1
    return 0


@on_startup(order=50)
async def seed_v4():
    if not config.SEED_DEMO or await db.system.find_one({"key": MARKER}):
        return
    delivered = await db.shipments.find({"demo": True, "status": "delivered"}, NO_ID).sort("delivered_at", 1).to_list(50)
    active = await db.shipments.find({"demo": True, "status": {"$in": ["picked_up", "in_transit", "at_hub", "customs_hold",
                                                                      "out_for_delivery", "exception"]}}, NO_ID).to_list(200)
    if not delivered:
        log.info("V4 seed: no delivered demo shipments yet (V1 seed has not run); will retry on next start")
        return
    inv = await _seed_invoices(delivered)
    # Shipments still on the way get their booked quote too, so delivering one later invoices it like a live booking.
    booked = await db.shipments.find({"demo": True, "status": {"$nin": ["delivered", "cancelled"]}, "quote_id": None},
                                     NO_ID).to_list(200)
    for s in booked:
        await _link_quote(s)
    threads, ai_thread = await _seed_threads(active, delivered)
    exc = await _seed_exceptions(active)
    cl = await _seed_claim(delivered)
    await db.system.update_one({"key": MARKER}, {"$set": {"at": now_iso(), "invoices": inv, "threads": threads,
                                                          "exceptions": exc, "claims": cl}}, upsert=True)
    log.info("V4 seed: %s invoices, %s threads, %s exceptions, %s claims", inv, threads, exc, cl)
    if ai_thread:
        from services.comms_agent import draft_reply, spawn

        spawn(draft_reply(ai_thread))  # one real AI draft (or "needs a person" when no model is configured)
