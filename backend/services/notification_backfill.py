"""Bilingual variants for notifications stored before core.notify took bi(fi, en) texts (s1 review r3, N4).

Old notifications have one plain title/body, mostly English ("Customs declaration drafted"), some Finnish only
(claim decisions, invoice e-mails in a Finnish invoice's locale). The patterns below are the formats those call sites
wrote; each gives (fi, en). Text the user wrote (message previews, notes) and unknown formats are left as they are and
counted. One-time startup repair (marker in db.system); the plain title/body are not changed.
"""
import logging
import re

from core.db import db
from core.lifecycle import on_startup
from core.util import now_iso

log = logging.getLogger(__name__)

MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
SEV_FI = {"critical": "kriittinen", "high": "korkea", "medium": "keskitaso", "low": "matala"}
CLAIM_TYPE_FI = {"damage": "vaurio", "loss": "katoaminen", "delay": "viivästys"}
CUSTOMS_EVENT_FI = {"rejected": "hylätty", "not released": "ei luovutettu", "under control": "tarkastuksessa"}
REASON_FI = {"total above the approval threshold": "summa ylittää hyväksyntärajan", "dangerous goods": "vaarallisia aineita",
             "no rates loaded for this lane": "reitille ei ole hinnastoa",
             "an option charges no freight (the Incoterm leaves the main carriage to the other party)":
                 "vaihtoehto ei sisällä rahtia (toimituslauseke jättää päärahdin toiselle osapuolelle)"}
VERIFY_FI = {"approved": "hyväksytty", "needs attention": "vaatii huomiota", "declined": "hylätty"}
STATUS_TITLES = {"Delivered": "Toimitettu", "Shipment exception": "Lähetyksen poikkeama", "Customs hold": "Tullipysäytys",
                 "Out for delivery": "Jakelussa"}


def _eur_fi(amount: str) -> str:
    """"1234.50 EUR" style amounts (claims, refunds wrote :.2f) -> 1 234,50 €."""
    v = float(amount.replace(",", ""))
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " €"


def _eur_en_to_fi(s: str) -> str:
    """€1,703.22 -> 1 703,22 € (billing._fmt_eur 'en')."""
    return re.sub(r"€([\d,]+\.\d{2})", lambda m: _eur_fi(m.group(1)), s)


def _eur_fi_to_en(s: str) -> str:
    """1 703,22 € -> €1,703.22 (billing._fmt_eur 'fi')."""
    def conv(m):
        v = float(m.group(1).replace(" ", "").replace(" ", "").replace(",", "."))
        return f"€{v:,.2f}"
    return re.sub(r"([\d  ]+,\d{2}) €", conv, s)


def _date_fi(s: str) -> str:
    """'24 Oct 2026' or '2026-10-24' -> 24.10.2026."""
    m = re.fullmatch(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})", s)
    if m and m.group(2) in MONTHS:
        return f"{int(m.group(1))}.{MONTHS[m.group(2)]}.{m.group(3)}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{int(m.group(3))}.{int(m.group(2))}.{m.group(1)}"
    return s


def _date_en(s: str) -> str:
    """'24.10.2026' -> '24 Oct 2026'."""
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if not m:
        return s
    names = {v: k for k, v in MONTHS.items()}
    return f"{int(m.group(1)):02d} {names[int(m.group(2))]} {m.group(3)}"


# (regex on the stored title, fi, en) for titles; groups are re-used as written.
TITLES = [
    (r"Payment needs review: (.*)", "Maksu tarkistettava: {0}", "Payment needs review: {0}"),
    (r"Claim (\S+) submitted", "Reklamaatio {0} jätetty", "Claim {0} submitted"),
    (r"Reklamaatio (\S+): hyväksytty", "Reklamaatio {0}: hyväksytty", "Claim {0}: approved"),
    (r"Reklamaatio (\S+): hylätty", "Reklamaatio {0}: hylätty", "Claim {0}: rejected"),
    (r"(Delivered|Shipment exception|Customs hold|Out for delivery): (.*)", None, None),
    (r"Needs a person: (.*)", "Vaatii ihmisen: {0}", "Needs a person: {0}"),
    (r"Invoice draft to review: (.*)", "Tarkistettava laskuluonnos: {0}", "Invoice draft to review: {0}"),
    (r"Lasku (\S+) – (.+)", "Lasku {0} – {1}", "Invoice {0} from {1}"),
    (r"Invoice (\S+) from (.+)", "Lasku {0} – {1}", "Invoice {0} from {1}"),
    (r"Maksumuistutus: lasku (\S+)", "Maksumuistutus: lasku {0}", "Payment reminder: invoice {0}"),
    (r"Payment reminder: invoice (\S+)", "Maksumuistutus: lasku {0}", "Payment reminder: invoice {0}"),
    (r"Maksu vastaanotettu: lasku (\S+)", "Maksu vastaanotettu: lasku {0}", "Payment received: invoice {0}"),
    (r"Payment received: invoice (\S+)", "Maksu vastaanotettu: lasku {0}", "Payment received: invoice {0}"),
    (r"Hyvityslasku (\S+)", "Hyvityslasku {0}", "Credit note {0}"),
    (r"Credit note (\S+)", "Hyvityslasku {0}", "Credit note {0}"),
    (r"Lasku (\S+) on mitätöity", "Lasku {0} on mitätöity", "Invoice {0} has been voided"),
    (r"Invoice (\S+) has been voided", "Lasku {0} on mitätöity", "Invoice {0} has been voided"),
    (r"Refund on (.*)", "Palautus laskulle {0}", "Refund on {0}"),
    (r"Overdue: (.*)", "Erääntynyt: {0}", "Overdue: {0}"),
    (r"(.+): tilannepäivitys", "{0}: tilannepäivitys", "{0}: status update"),
    (r"(.+): status update", "{0}: tilannepäivitys", "{0}: status update"),
    (r"Uusi viesti: (.*)", "Uusi viesti: {0}", "New message: {0}"),
    (r"New message: (.*)", "Uusi viesti: {0}", "New message: {0}"),
    (r"(Critical|High|Medium|Low) exception: (.*)", None, None),
    (r"Quote to review: (.*)", "Tarkistettava tarjous: {0}", "Quote to review: {0}"),
    (r"Quote (\S+) is ready", "Tarjous {0} on valmis", "Quote {0} is ready"),
    (r"New booking ?(.*)", "Uusi varaus {0}", "New booking {0}"),
    (r"Quote (\S+) could not be confirmed", "Tarjousta {0} ei voitu vahvistaa", "Quote {0} could not be confirmed"),
    (r"KYC review: (.*)", "Asiakastuntemuksen tarkistus: {0}", "KYC review: {0}"),
    (r"Company verification (approved|needs attention|declined)", None, None),
    (r"Customs draft (\S+) \((.*)\)", "Tulli-ilmoitusluonnos {0} ({1})", "Customs draft {0} ({1})"),
    (r"Customs declaration drafted", "Tulli-ilmoitus luonnosteltu", "Customs declaration drafted"),
    (r"(\S+) ready for review", "{0} valmis tarkistettavaksi", "{0} ready for review"),
    (r"Customs: (rejected|not released|under control) \((.*)\)", None, None),
    (r"Released by Customs: (.*)", "Tulli luovutti: {0}", "Released by Customs: {0}"),
]


def title_variants(title: str) -> dict | None:
    for pat, fi, en in TITLES:
        m = re.fullmatch(pat, title or "")
        if not m:
            continue
        g = [x.strip() for x in m.groups()]
        if pat.startswith("(Delivered"):
            return {"fi": f"{STATUS_TITLES[g[0]]}: {g[1]}", "en": title}
        if pat.startswith("(Critical"):
            return {"fi": f"Poikkeama ({SEV_FI[g[0].lower()]}): {g[1]}", "en": title}
        if pat.startswith("Company verification"):
            return {"fi": f"Yrityksen tunnistus {VERIFY_FI[g[0]]}", "en": title}
        if pat.startswith("Customs: "):
            return {"fi": f"Tulli: {CUSTOMS_EVENT_FI[g[0]]} ({g[1]})", "en": title}
        return {"fi": fi.format(*g).strip(), "en": en.format(*g).strip()}
    return None


# (regex on the stored body, function(match) -> (fi, en)); bodies the user wrote are not listed.
BODIES = [
    (r"amount (\d+) (\w+) differs from expected (\S+) eur",
     lambda m: (f"Maksettu summa {m[1]} {m[2].upper()} (sentteinä) poikkeaa odotetusta {m[3]}", m[0])),
    (r"(.+) · (damage|loss|delay) · ([\d.,]+) EUR",
     lambda m: (f"{m[1]} · {CLAIM_TYPE_FI[m[2]]} · {_eur_fi(m[3])}", m[0])),
    (r"Korvaus ([\d.,]+) EUR", lambda m: (f"Korvaus {_eur_fi(m[1])}", f"Compensation {m[1]} EUR")),
    (r"(\S+) was delivered to (.+)\.", lambda m: (f"{m[1]} on toimitettu kohteeseen {m[2]}.", m[0])),
    (r"(\S+) is on its way to (.+)\.", lambda m: (f"{m[1]} on matkalla kohteeseen {m[2]}.", m[0])),
    (r"(\S+) is held in customs: (.*)", lambda m: (f"{m[1]} on pysäytetty tullissa: {m[2]}", m[0])),
    (r"AI is not configured: .*", lambda m: ("AI-yhteyttä ei ole otettu käyttöön; vastaa käsin.", m[0])),
    (r"AI draft failed: (.*)", lambda m: (f"AI-luonnos epäonnistui: {m[1]}", m[0])),
    (r"Review and issue the invoice\.", lambda m: ("Tarkista ja lähetä lasku.", m[0])),
    (r"Lasku (\S+) lähetykselle (\S+)\.", lambda m: (m[0], f"Invoice {m[1]} for shipment {m[2]}.")),
    (r"Invoice (\S+) for shipment (\S+)\.", lambda m: (f"Lasku {m[1]} lähetykselle {m[2]}.", m[0])),
    (r"Lasku (\S+)\.", lambda m: (m[0], f"Invoice {m[1]}.")),
    (r"Invoice (\S+)\.", lambda m: (f"Lasku {m[1]}.", m[0])),
    (r"Laskun (\S+) eräpäivä (\S+) on ohitettu\.", lambda m: (m[0], f"Invoice {m[1]} was due on {_date_en(m[2])}.")),
    (r"Invoice (\S+) was due on (.+)\.", lambda m: (f"Laskun {m[1]} eräpäivä {_date_fi(m[2])} on ohitettu.", m[0])),
    (r"Kiitos, vastaanotimme maksun (.+) laskulle (\S+)\.",
     lambda m: (m[0], f"Thank you, we received {_eur_fi_to_en(m[1])} for invoice {m[2]}.")),
    (r"Thank you, we received (.+) for invoice (\S+)\.",
     lambda m: (f"Kiitos, vastaanotimme maksun {_eur_en_to_fi(m[1])} laskulle {m[2]}.", m[0])),
    (r"Hyvityslasku (\S+) laskulle (\S*), summa (.+)\.",
     lambda m: (m[0], f"Credit note {m[1]} for invoice {m[2]}, amount {_eur_fi_to_en(m[3])}.")),
    (r"Credit note (\S+) for invoice (\S*), amount (.+)\.",
     lambda m: (f"Hyvityslasku {m[1]} laskulle {m[2]}, summa {_eur_en_to_fi(m[3])}.", m[0])),
    (r"Lasku (\S+) on mitätöity, eikä sitä tarvitse maksaa\.", lambda m: (m[0], f"Invoice {m[1]} has been voided; no payment is due.")),
    (r"Invoice (\S+) has been voided; no payment is due\.", lambda m: (f"Lasku {m[1]} on mitätöity, eikä sitä tarvitse maksaa.", m[0])),
    (r"([\d.,]+) EUR refunded in Stripe\. Issue a credit note if the charge is not owed\.",
     lambda m: (f"{_eur_fi(m[1])} palautettu Stripessä. Tee hyvityslasku, jos veloitus ei ole aiheellinen.", m[0])),
    (r"(.+) · (€[\d,]+\.\d{2}) was due (\d{4}-\d{2}-\d{2})",
     lambda m: (f"{m[1]} · {_eur_en_to_fi(m[2])}, eräpäivä {_date_fi(m[3])}", m[0])),
    (r"(.+) — (.+)", None),  # quote review: "<route> — <reasons>", only when every reason is known
    (r"Contact us for alternatives\.", lambda m: ("Ota yhteyttä, niin etsimme vaihtoehdon.", m[0])),
    (r"(\d+) goods items drafted for (.*)",
     lambda m: (("1 tavaranimike luonnosteltu" if m[1] == "1" else f"{m[1]} tavaranimikettä luonnosteltu") + f": {m[2]}", m[0])),
    (r"Check the goods data for your shipment", lambda m: ("Tarkista lähetyksesi tavaratiedot", m[0])),
    (r"Check the goods data for (.+)", lambda m: (f"Tarkista lähetyksen {m[1]} tavaratiedot", m[0])),
    (r"(.+) confirmed the goods data", lambda m: (f"{m[1]} vahvisti tavaratiedot", m[0])),
]


def body_variants(body: str) -> dict | None:
    for pat, fn in BODIES:
        m = re.fullmatch(pat, body or "")
        if not m:
            continue
        if fn is None:  # quote review reasons
            reasons = m[2].split(", ")
            if not all(r in REASON_FI for r in reasons):
                continue
            return {"fi": f"{m[1]} — " + ", ".join(REASON_FI[r] for r in reasons), "en": body}
        fi, en = fn(m)
        return {"fi": fi, "en": en}
    return None


async def _exception_body(n: dict) -> dict | None:
    """Exception alerts carried the exception's title as the body: use its stored variants."""
    m = re.search(r"/ops/exceptions\?id=([\w-]+)", n.get("link") or "")
    ex = await db.exceptions.find_one({"id": m.group(1)}, {"_id": 0, "title": 1, "title_i18n": 1}) if m else None
    v = (ex or {}).get("title_i18n") or {}
    return {"fi": v["fi"], "en": v.get("en") or n["body"]} if ex and v.get("fi") and ex.get("title") == n.get("body") else None


async def _kyc_body(n: dict) -> dict | None:
    """KYC review alerts listed "<check>: <summary>"; the company's checks hold summary_fi for the same summary."""
    from services.kyc import CHECK_FI

    m = re.search(r"/ops/customers/([\w-]+)", n.get("link") or "")
    c = await db.companies.find_one({"id": m.group(1)}, {"_id": 0, "kyc": 1}) if m else None
    kyc = (c or {}).get("kyc") or {}
    parts = []
    for part in (n.get("body") or "").split("; "):
        key, _, summary = part.partition(": ")
        chk = kyc.get(key) or {}
        if not summary or chk.get("summary") != summary or not chk.get("summary_fi"):
            return None
        parts.append(f"{CHECK_FI.get(key, key)}: {chk['summary_fi']}")
    return {"fi": "; ".join(parts), "en": n["body"]} if parts else None


async def variants_for(n: dict) -> dict:
    """$set for one stored notification (empty when nothing is known)."""
    out = {}
    if not n.get("title_i18n"):
        t = title_variants(n.get("title") or "")
        if t:
            out["title_i18n"] = t
    if n.get("body") and not n.get("body_i18n"):
        b = body_variants(n["body"])
        if not b and re.fullmatch(r"(Critical|High|Medium|Low) exception: .*", n.get("title") or ""):
            b = await _exception_body(n)
        if not b and (n.get("title") or "").startswith("KYC review: "):
            b = await _kyc_body(n)
        if b:
            out["body_i18n"] = b
    return out


@on_startup(order=71)
async def backfill_notification_variants():
    marker = "r3-notification-variants:1"
    if await db.system.find_one({"key": marker}):
        return
    counts = {"updated": 0, "unknown_titles": 0, "seen": 0}
    unknown = {}
    async for n in db.notifications.find({"$or": [{"title_i18n": None}, {"body_i18n": None}]}, {"_id": 0}):
        counts["seen"] += 1
        upd = await variants_for(n)
        if upd:
            await db.notifications.update_one({"id": n["id"]}, {"$set": upd})
            counts["updated"] += 1
        if "title_i18n" not in upd and not n.get("title_i18n"):
            counts["unknown_titles"] += 1
            unknown[n.get("title") or ""] = unknown.get(n.get("title") or "", 0) + 1
    log.info("notification variants backfill: %s; unknown titles (first 10): %s", counts, list(unknown)[:10])
    await db.system.insert_one({"key": marker, "at": now_iso(), **counts})
