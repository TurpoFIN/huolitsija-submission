"""E-mail texts of the demo. Tone: an established forwarder, short and precise. Rate requests describe the shipment
and the ask; they never state a booking, a contract or a volume that does not exist."""
from __future__ import annotations

from datetime import date

from core import config
from services import gazetteer

MODE_SERVICE = {"road": "Road freight, door to door", "sea": "Sea freight, door to door", "air": "Air freight, door to door",
                "rail": "Rail freight, door to door"}
QUESTION = {
    "cargo.description": "What are the goods (a short description)?",
    "cargo.packages": "How many packages, and what kind (e.g. EUR pallets, crates, cartons)?",
    "weight_kg": "What is the total gross weight in kg?",
    "volume_m3": "What is the total volume, or the package dimensions (L × W × H)?",
    "ready_date": "When are the goods ready for pickup?",
    "incoterm": "Which Incoterm applies (e.g. EXW, FCA, DAP, DDP) and the named place?",
    "pickup_address": "What is the exact pickup address (street, postcode, city)?",
    "delivery_address": "What is the exact delivery address (street, postcode, city)?",
    "dangerous_goods": "Are any of the goods dangerous goods (UN number and class if so)?",
}


def days(n) -> str:
    return "—" if n in (None, "") else f"{n} day" + ("" if int(n) == 1 else "s")


def eur(cents: int | None) -> str:
    return "—" if cents is None else f"EUR {cents / 100:,.2f}"


def place(p: dict | None) -> str:
    p = p or {}
    country = gazetteer.country_name(p.get("country")) or p.get("country") or ""
    return f"{p.get('name') or p.get('city') or '—'}, {country}".strip(", ")


def day(iso: str | None) -> str:
    try:
        return date.fromisoformat((iso or "")[:10]).strftime("%-d %B %Y")
    except ValueError:
        return iso or "—"


def cargo_line(req: dict, extras: dict) -> str:
    c = req.get("cargo") or {}
    parts = []
    if c.get("description"):
        parts.append(c["description"])
    if c.get("containers"):
        parts.append(", ".join(f"{x['count']} × {x['type']}" for x in c["containers"]))
    elif c.get("packages"):
        from services.pricing import PACKAGE_LABEL
        parts.append(f"{c['packages']} × {PACKAGE_LABEL.get(c.get('package_type') or 'other', 'packages')}")
    if c.get("weight_kg"):
        parts.append(f"{c['weight_kg']:,.0f} kg gross")
    if c.get("volume_m3"):
        parts.append(f"{c['volume_m3']:,.1f} m³")
    if extras.get("dimensions"):
        parts.append(extras["dimensions"])
    parts.append("stackable" if c.get("stackable") else "non-stackable" if c.get("stackable") is False and extras.get("stackable_stated") else "")
    if c.get("dangerous_goods"):
        dg = c.get("dg") or {}
        parts.append("DG " + " ".join(x for x in [dg.get("un_number"), f"class {dg.get('class')}" if dg.get("class") else None] if x))
    else:
        parts.append("non-DG")
    return ", ".join(p for p in parts if p)


def clarification(run: dict, req: dict, missing: list[str], round_no: int) -> tuple[str, list[str]]:
    lane = f"{place(req.get('origin'))} → {place(req.get('destination'))}"
    subject = f"Your quote request {lane} [{run['ref']}]"
    if round_no > 1:
        subject = f"Re: {subject}"
    qs = [QUESTION.get(m, m) for m in missing]
    lines = [f"Thank you for your request for {lane}.",
             "To price it precisely, we need a few details:" if round_no == 1 else "Thanks. One more detail and we can price it:",
             *[f"{i}. {q}" for i, q in enumerate(qs, 1)],
             "Just reply to this e-mail. We start sourcing capacity on your lane right away.",
             "Huolitsija Quotes"]
    return subject, lines


def rfq(run: dict, row: dict, req: dict, extras: dict, mode: str) -> tuple[str, list[str]]:
    o, d = req.get("origin") or {}, req.get("destination") or {}
    ready = day(req.get("ready_date"))
    subject = f"[{row['ref']}] Rate request: {mode} {o.get('name')} ({o.get('country')}) → {d.get('name')} ({d.get('country')}), ready {ready}"
    pickup = extras.get("pickup_address") or place(o)
    delivery = extras.get("delivery_address") or place(d)
    inc = f"{req.get('incoterm')} {req.get('incoterm_place') or ''}".strip() if req.get("incoterm") else None
    lines = [f"Hello {row['carrier_name']} team,",
             f"Huolitsija is a digital freight forwarder and customs broker in Helsinki. We are quoting a shipment "
             f"on your lane and would like your rate.",
             f"Route: pickup {pickup} → delivery {delivery}",
             f"Service: {MODE_SERVICE.get(mode, mode)}",
             f"Cargo: {cargo_line(req, extras)}",
             f"Ready for pickup: {ready}",
             *([f"Terms: {inc} (Incoterms 2020)"] if inc else []),
             "Please reply with your all-in rate in EUR (or base rate and surcharges), transit time and validity.",
             "Thank you,",
             f"Huolitsija Sourcing · {config.COMPANY_ADDRESS}"]
    return subject, lines


def follow_up(row: dict, kind: str, body: str) -> tuple[str, list[str]]:
    subject = f"Re: [{row['ref']}] Rate request"
    return subject, [*[x for x in body.split("\n") if x.strip()], "Huolitsija Sourcing"]


def counteroffer(row: dict, amount_cents: int, best_other: int | None) -> tuple[str, list[str]]:
    lines = [f"Thanks for your rate. We are close: if you can do {eur(amount_cents)} all-in for this shipment, "
             f"we put your rate forward to the shipper today.",
             "Same route, cargo and dates as in our request.", "Huolitsija Sourcing"]
    return f"Re: [{row['ref']}] Rate request", lines


def book_lines(terms_url: str | None) -> list[str]:
    return ["To book, reply BOOK to this e-mail with the shipper's and consignee's name and street address. "
            "You can attach the commercial invoice and packing list.",
            f"By booking you accept NSAB 2015 (PSYM 2015){' and our terms: ' + terms_url if terms_url else ''}. "
            "Duties and import VAT are not included unless listed."]


def offer(run: dict, quote: dict, req: dict, extras: dict, pick: dict) -> tuple[str, list[str], str]:
    o, d = req.get("origin") or {}, req.get("destination") or {}
    lane = f"{place(o)} → {place(d)}"
    subject = f"Quote {quote['number']}: {lane}, {eur(pick['sell_cents'])} [{run['ref']}]"
    inc = f"{req.get('incoterm')} {req.get('incoterm_place') or ''}".strip() if req.get("incoterm") else None
    lines = [f"Here is your quote for {lane}.",
             f"Service: {MODE_SERVICE.get(pick['mode'], pick['mode'])}",
             f"Pickup: {extras.get('pickup_address') or place(o)}",
             f"Delivery: {extras.get('delivery_address') or place(d)}",
             f"Cargo: {cargo_line(req, extras)}",
             *[f"{line['description']}: {eur(line['amount_cents'])}" for line in pick["lines"]],
             f"Total: {eur(pick['sell_cents'])} excl. VAT",
             f"Transit time: {days(pick['transit_days'])} from pickup on {day(req.get('ready_date'))}",
             f"Valid until: {day(pick['valid_until'])}",
             f"Terms: {inc + ' (Incoterms 2020) · ' if inc else ''}NSAB 2015",
             "Huolitsija Quotes"]
    return subject, lines, f"Quote {quote['number']}"
