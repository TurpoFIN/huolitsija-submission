"""Quote PDF (reportlab): company header, request summary, options with price breakdown, VAT treatment,
validity, liability regime and the NSAB 2015 (PSYM 2015) terms line (domain-spec RULE N-1, N-2, V-4, Q-1)."""
from __future__ import annotations

import io
import os
from datetime import date
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core import config
from services.pricing import LIABILITY, REASON_FI

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"


def _register_fonts():
    global _FONT, _FONT_BOLD
    candidates = [("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")]
    import reportlab

    rl = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    candidates.append((os.path.join(rl, "Vera.ttf"), os.path.join(rl, "VeraBd.ttf")))
    for reg, bold in candidates:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("HuoSans", reg))
                pdfmetrics.registerFont(TTFont("HuoSans-Bold", bold))
                _FONT, _FONT_BOLD = "HuoSans", "HuoSans-Bold"
                return
            except Exception:  # noqa: BLE001
                continue


_register_fonts()

# RULE V-4: legal basis texts come from configuration (to be approved by the tax adviser). The zero-rate wordings
# are read with services.billing.configured_vat_text, so quotes and invoices use the same env names
# (VAT_TEXT_ZERO_INTL_TRANSPORT_*, alias VAT_TEXT_ZERO_INTL_*; VAT_TEXT_ZERO_EXPORT_*); below are the defaults.
VAT_TEXT = {
    "en": {
        "ZERO_INTL_TRANSPORT": "0 %: international transport and related services (Finnish VAT Act 70-72 §; wording pending tax adviser review)",
        "ZERO_EXPORT": "0 %: services related to exported goods (Finnish VAT Act 70-71 §)",
        "FI_25_5": "25.5 %: Finnish domestic service",
        "FI_13_5": "13.5 %: reduced rate",
        "FI_14": "13.5 %: reduced rate",
        "FI_10": "10 %: reduced rate",
        "OUT_OF_SCOPE_DISBURSEMENT": "Outside the scope of VAT: paid on your behalf (duties, taxes, insurance premium)",
        "EU_ICS_RC": "Reverse charge: buyer accounts for VAT (intra-EU service)",
        "OUT_OF_SCOPE_FOREIGN": "Outside Finnish VAT: place of supply abroad",
    },
    "fi": {
        "ZERO_INTL_TRANSPORT": "0 %: kansainvälinen kuljetus ja siihen liittyvät palvelut (AVL 70-72 §; sanamuoto veroneuvojan tarkistettavana)",
        "ZERO_EXPORT": "0 %: vientitavaraan liittyvät palvelut (AVL 70-71 §)",
        "FI_25_5": "25,5 %: kotimainen palvelu",
        "FI_13_5": "13,5 %: alennettu verokanta",
        "FI_14": "13,5 %: alennettu verokanta",
        "FI_10": "10 %: alennettu verokanta",
        "OUT_OF_SCOPE_DISBURSEMENT": "Ei arvonlisäveron piirissä: maksetaan puolestanne (tullit, verot, vakuutusmaksu)",
        "EU_ICS_RC": "Käännetty verovelvollisuus: ostaja tilittää veron (EU-palvelu)",
        "OUT_OF_SCOPE_FOREIGN": "Ei Suomen arvonlisäveroa: myyntimaa ulkomailla",
    },
}


def vat_text(code: str, lang: str) -> str:
    """Approved wording from env (shared with invoices), else the quote default for this code."""
    from services.billing import configured_vat_text

    cfg = configured_vat_text(code)
    return cfg[lang] if cfg else VAT_TEXT[lang].get(code, code)

T = {
    "en": {"quote": "Quote", "date": "Date", "valid": "Valid until", "customer": "Customer", "route": "Route", "mode": "Mode",
           "cargo": "Cargo", "incoterm": "Incoterm", "ready": "Cargo ready", "option": "Option", "recommended": "recommended",
           "etd": "ETD", "eta": "ETA", "transit": "Transit", "days": "days", "co2": "CO2e (GLEC default factors)",
           "desc": "Description", "qty": "Basis x qty", "unit": "Unit price", "amount": "Amount", "vat": "VAT",
           "subtotal": "Total excl. VAT", "vat_est": "VAT (estimate)", "total": "Total incl. VAT", "not_incl": "Not included",
           "liability": "Liability", "vat_treatment": "VAT treatment", "terms": "Terms", "terms_v": "Huolitsija terms version",
           "terms_text": "Services are performed according to NSAB 2015 (PSYM 2015), the General Conditions of the Nordic "
                         "Association of Freight Forwarders, published in Finland by Suomen Huolinta- ja Logistiikkaliitto ry. "
                         "Huolitsija acts as contracting party (principal) for the transport it quotes.",
           "indicative": "Indicative rates at 2026 market levels, not a carrier contract. The booked price is confirmed by "
                         "Huolitsija; surcharges follow carrier notices. Prices in EUR.",
           "estimated": "estimated", "pending": "pending registration", "page": "Page", "none": "No options yet: our pricing team prepares this quote.",
           "message": "Message from Huolitsija", "warnings": "Notes", "business_id": "Business ID", "accepted": "accepted",
           "modes": {"sea": "Sea", "road": "Road", "air": "Air", "rail": "Rail"}},
    "fi": {"quote": "Tarjous", "date": "Päiväys", "valid": "Voimassa asti", "customer": "Asiakas", "route": "Reitti", "mode": "Kuljetusmuoto",
           "cargo": "Rahti", "incoterm": "Toimitusehto", "ready": "Rahti valmiina", "option": "Vaihtoehto", "recommended": "suositeltu",
           "etd": "Lähtö", "eta": "Saapuminen", "transit": "Kuljetusaika", "days": "pv", "co2": "CO2e (GLEC-oletuskertoimet)",
           "desc": "Kuvaus", "qty": "Peruste x määrä", "unit": "Yksikköhinta", "amount": "Summa", "vat": "Alv",
           "subtotal": "Yhteensä alv 0 %", "vat_est": "Arvonlisävero (arvio)", "total": "Yhteensä sis. alv", "not_incl": "Ei sisälly",
           "liability": "Vastuu", "vat_treatment": "Arvonlisäverokäsittely", "terms": "Ehdot", "terms_v": "Huolitsijan ehtojen versio",
           "terms_text": "Palvelut suoritetaan Pohjoismaisen Speditööriliiton yleisten määräysten NSAB 2015 mukaisesti "
                         "(Suomessa PSYM 2015, julkaisija Suomen Huolinta- ja Logistiikkaliitto ry). Huolitsija toimii tarjoamiensa "
                         "kuljetusten sopimusosapuolena (päämiehenä).",
           "indicative": "Ohjeelliset hinnat vuoden 2026 markkinatasolla, ei kuljetusliikkeen sopimushinta. Huolitsija vahvistaa "
                         "varatun hinnan; lisämaksut seuraavat kuljetusliikkeiden ilmoituksia. Hinnat euroina.",
           "estimated": "arvio", "pending": "rekisteröinti vireillä", "page": "Sivu", "none": "Ei vielä vaihtoehtoja: hinnoittelutiimimme laatii tarjouksen.",
           "message": "Viesti Huolitsijalta", "warnings": "Huomiot", "business_id": "Y-tunnus", "accepted": "hyväksytty",
           "modes": {"sea": "Meri", "road": "Maantie", "air": "Lento", "rail": "Rautatie"}},
}


BASIS = {
    "en": {"PER_CONTAINER": "container", "PER_SHIPMENT": "shipment", "PER_BL": "B/L", "PER_KG": "kg", "PER_WM": "W/M", "PER_100KG": "100 kg",
           "PER_LDM": "ldm", "PER_KM": "km", "PER_TRUCK": "truck", "PERCENT": "%", "PER_DECLARATION": "declaration",
           "PER_FILING": "filing", "PER_ITEM": "item", "ACTUAL": "actual", "FIXED": "fixed", "PER_PALLET": "pallet"},
    "fi": {"PER_CONTAINER": "kontti", "PER_SHIPMENT": "lähetys", "PER_BL": "konossementti", "PER_KG": "kg", "PER_WM": "W/M", "PER_100KG": "100 kg",
           "PER_LDM": "lvm", "PER_KM": "km", "PER_TRUCK": "auto", "PERCENT": "%", "PER_DECLARATION": "ilmoitus",
           "PER_FILING": "ilmoitus", "PER_ITEM": "nimike", "ACTUAL": "toteutunut", "FIXED": "kiinteä", "PER_PALLET": "lava"},
}
LIABILITY_FI = {k: v["label_fi"] for k, v in LIABILITY.items()}


def E(value) -> str:
    """Escape user or ops supplied text for reportlab's mini-markup (<, >, & would break or inject tags)."""
    return escape("" if value is None else str(value))


def _eur(cents: int | None, lang: str) -> str:
    if cents is None:
        return "-"
    s = f"{cents / 100:,.2f}"
    if lang == "fi":
        s = s.replace(",", " ").replace(".", ",")
    return f"{s} EUR"


def _num(v, lang: str, digits: int = 0) -> str:
    if v is None:
        return "-"
    s = f"{v:,.{digits}f}"
    return s.replace(",", " ").replace(".", ",") if lang == "fi" else s


def _d(iso: str | None, lang: str) -> str:
    if not iso:
        return "-"
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.day}.{d.month}.{d.year}" if lang == "fi" else d.strftime("%d %b %Y")


def _loc(l: dict | None) -> str:
    if not l:
        return "-"
    code = l.get("unlocode")
    return f"{l.get('name')} ({code})" if code and code not in (l.get("name") or "") else (l.get("name") or "-")


def public_terms_url() -> str | None:
    """The terms page, or None when the app runs on a local address nobody outside can open."""
    from urllib.parse import urlparse
    base = config.APP_BASE_URL or "https://huolitsija.fi"
    host = (urlparse(base).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
        return None
    return base.rstrip("/") + "/terms"


async def render_quote_pdf(q: dict, company: dict | None, *, lang: str = "fi") -> bytes:
    t = T[lang]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"{t['quote']} {q.get('number') or ''}", author=config.COMPANY_NAME)
    base = ParagraphStyle("b", fontName=_FONT, fontSize=8.5, leading=11, textColor=colors.HexColor("#1a1a1a"))
    small = ParagraphStyle("s", parent=base, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=base, fontName=_FONT_BOLD, fontSize=14, leading=18)
    h2 = ParagraphStyle("h2", parent=base, fontName=_FONT_BOLD, fontSize=10, leading=13, spaceBefore=8, spaceAfter=3)
    right = ParagraphStyle("r", parent=base, alignment=2)
    story = []

    bid = config.COMPANY_BUSINESS_ID  # no line at all until the company has one
    head = Table([[Paragraph(f"<b>{E(config.COMPANY_NAME)}</b><br/>{E(config.COMPANY_ADDRESS)}<br/>{E(config.COMPANY_EMAIL)}"
                             f"{' · ' + config.COMPANY_PHONE if config.COMPANY_PHONE else ''}"
                             f"{'<br/>' + t['business_id'] + ': ' + E(bid) if bid else ''}", small),
                   Paragraph(f"<b>{t['quote']} {q.get('number') or ''}</b><br/>{t['date']}: {_d(q.get('priced_at') or q.get('created_at'), lang)}"
                             f"<br/><b>{t['valid']}: {_d(q.get('valid_until'), lang)}</b>", right)]],
                 colWidths=[100 * mm, 78 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 6 * mm), Paragraph(f"{t['quote']} {q.get('number') or ''}", h1), Spacer(1, 2 * mm)]

    req = q.get("request") or {}
    inc = E(f"{req.get('incoterm')} {req.get('incoterm_place') or ''}".strip()) if req.get("incoterm") else "-"
    opt_modes = sorted({o.get("mode") for o in q.get("options") or [] if o.get("mode")})
    modes = [req["mode"]] if req.get("mode") else opt_modes
    facts = [[t["customer"], E((company or {}).get("name") or q.get("customer_name") or "-")],
             [t["route"], E(f"{_loc(req.get('origin'))} - {_loc(req.get('destination'))}")],
             [t["mode"], ", ".join(t["modes"].get(m, m) for m in modes) or "-"],
             [t["cargo"], E(req.get("cargo_summary") or "-")], [t["incoterm"], inc], [t["ready"], _d(req.get("ready_date"), lang)]]
    ft = Table([[Paragraph(a, small), Paragraph(str(b), base)] for a, b in facts], colWidths=[32 * mm, 146 * mm])
    ft.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 1)]))
    story.append(ft)
    if q.get("customer_message"):
        story += [Paragraph(t["message"], h2), Paragraph(E(q["customer_message"]).replace("\n", "<br/>"), base)]

    options = q.get("options") or []
    if q.get("accepted_option_id"):
        options = [o for o in options if o["id"] == q["accepted_option_id"]] or options
    if not options:
        story += [Spacer(1, 4 * mm), Paragraph(t["none"], base)]
    used_vat = set()
    all_firm = bool(options) and all(o.get("firmness") == "firm" for o in options)
    for i, o in enumerate(options, 1):
        # white_label: Huolitsija sells the service as its own; the carrier stays internal.
        name = " · ".join(E(x) for x in ([] if o.get("white_label") else [o.get("carrier_name")]) + [o.get("service_name")] if x)
        if len(options) == 1:
            heading = name
        else:
            tag = f" ({t['accepted']})" if o["id"] == q.get("accepted_option_id") else (f" ({t['recommended']})" if o.get("recommended") else "")
            heading = f"{t['option']} {i}: {name}{tag}"
        block = [Paragraph(heading, h2),
                 Paragraph(f"{t['etd']} {_d(o.get('etd'), lang)} · {t['eta']} {_d(o.get('eta'), lang)} · {t['transit']} "
                           f"{o.get('transit_days')} {t['days']} · {t['co2']} {_num(o.get('co2e_kg'), lang)} kg", small),
                 Spacer(1, 1.5 * mm)]
        rows = [[Paragraph(f"<b>{t['desc']}</b>", small), Paragraph(f"<b>{t['qty']}</b>", small), Paragraph(f"<b>{t['unit']}</b>", small),
                 Paragraph(f"<b>{t['amount']}</b>", small), Paragraph(f"<b>{t['vat']}</b>", small)]]
        for ln in o.get("lines") or []:
            used_vat.add(ln.get("vat_code"))
            desc = E(ln.get("description_fi") if lang == "fi" and ln.get("description_fi") else ln.get("description"))
            if ln.get("estimated"):
                desc += f" ({t['estimated']})"
            note = ln.get("note_fi") if lang == "fi" and ln.get("note_fi") else ln.get("note")
            if note:
                desc += f"<br/><font size=6.5 color='#666666'>{E(note)}</font>"
            basis = BASIS[lang].get(ln.get("basis") or "", (ln.get("basis") or "").replace("PER_", "").replace("_", " ").lower())
            rows.append([Paragraph(desc, base), Paragraph(f"{basis} x {_num(ln.get('qty'), lang, 2 if (ln.get('qty') or 0) % 1 else 0)}", small),
                         Paragraph(_eur(ln.get("unit_price_cents"), lang), right), Paragraph(_eur(ln.get("amount_cents"), lang), right),
                         Paragraph(f"{_num(ln.get('vat_rate_pct'), lang, 1)} %", right)])
        rows += [[Paragraph(t["subtotal"], base), "", "", Paragraph(f"<b>{_eur(o.get('total_cents'), lang)}</b>", right), ""],
                 [Paragraph(t["vat"] if all_firm else t["vat_est"], base), "", "", Paragraph(_eur(o.get("vat_cents"), lang), right), ""],
                 [Paragraph(f"<b>{t['total']}</b>", base), "", "", Paragraph(f"<b>{_eur(o.get('total_incl_vat_cents'), lang)}</b>", right), ""]]
        tbl = Table(rows, colWidths=[82 * mm, 28 * mm, 26 * mm, 28 * mm, 14 * mm], repeatRows=1)
        n = len(rows)
        tbl.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#999999")),
            ("LINEABOVE", (0, n - 3), (-1, n - 3), 0.5, colors.HexColor("#999999")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        block.append(tbl)
        if o.get("not_included"):
            ni = []
            for n_ in o["not_included"]:
                d = n_.get("description_fi") if lang == "fi" and n_.get("description_fi") else n_.get("description")
                amt = f" ~{_eur(n_['amount_cents'], lang)}" if n_.get("amount_cents") else ""
                reason = n_.get("reason")
                if lang == "fi" and n_.get("reason_code") in REASON_FI:
                    reason = REASON_FI[n_["reason_code"]].format(inc=req.get("incoterm") or "")
                ni.append(f"{E(d)}{amt}: {E(reason)}")
            block.append(Paragraph(f"<b>{t['not_incl']}:</b> " + "; ".join(ni), small))
        liab = (o.get("liability") or {}).get("label")
        if lang == "fi":
            liab = LIABILITY_FI.get(o.get("liability_regime"), liab)
        if liab:
            tail = ("Huolitsijan vastuu NSAB 2015 (PSYM 2015) mukaan; kuljetusmuodon pakottava sääntely soveltuu (verkostoperiaate)."
                    if lang == "fi" else "Forwarder liability under NSAB 2015 (PSYM 2015); mandatory law of the mode applies (network principle).")
            block.append(Paragraph(f"<b>{t['liability']}:</b> {liab}. {tail}", small))
        story.append(KeepTogether(block))

    warns = [w for w in q.get("warnings") or [] if w.get("level") in ("warning", "error")]
    if warns:
        story += [Paragraph(t["warnings"], h2)] + [Paragraph(f"• {E(w.get('message'))}", small) for w in warns]
    if used_vat:
        story += [Paragraph(t["vat_treatment"], h2)] + [Paragraph(E(vat_text(c, lang)), small) for c in sorted(used_vat) if c]
    terms_url = public_terms_url()
    story += [Paragraph(t["terms"], h2),
              Paragraph(t["terms_text"] + (f" {t['terms_v']} {E(str(q['terms_version']).split(' · ')[0])}." if q.get("terms_version") else "")
                        + (f" {E(terms_url)}" if terms_url else ""), small)]
    if not all_firm:  # firm partner rates are not indicative
        story += [Spacer(1, 2 * mm), Paragraph(t["indicative"], small)]

    def _footer(canvas, d):
        canvas.saveState()
        canvas.setFont(_FONT, 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(16 * mm, 10 * mm, " · ".join([config.COMPANY_NAME, *([f"{t['business_id']}: {bid}"] if bid else []), "NSAB 2015 (PSYM 2015)"]))
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"{t['page']} {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
