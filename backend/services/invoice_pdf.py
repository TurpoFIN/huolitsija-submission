"""Invoice / credit note PDF (reportlab), bilingual Finnish/English labels.

Covers the Finnish invoice content requirements (AVL 209 e §): issue date, unique number, seller and buyer
names/addresses, seller VAT id (and buyer VAT id for reverse charge), date of supply, quantity and nature of the
service, unit price excl. VAT, VAT rate and amount per rate, the exemption / reverse-charge basis, and for credit
notes the original invoice. Adds the payment slip facts Finnish customers expect: IBAN/BIC, viitenumero + RF
reference, due date, late-payment interest and the bank "virtuaaliviivakoodi" (version 4) with its Code 128 bar.
"""
from __future__ import annotations

import io
import os
from datetime import date
from decimal import Decimal

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core import config

_FONT, _FONT_B = "Helvetica", "Helvetica-Bold"


def _register_fonts():
    global _FONT, _FONT_B
    import reportlab

    candidates = [
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        (os.path.join(os.path.dirname(reportlab.__file__), "fonts", "Vera.ttf"),
         os.path.join(os.path.dirname(reportlab.__file__), "fonts", "VeraBd.ttf")),
    ]
    for reg, bold in candidates:
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("HuoSans", reg))
                pdfmetrics.registerFont(TTFont("HuoSans-Bold", bold))
                _FONT, _FONT_B = "HuoSans", "HuoSans-Bold"
                return
            except Exception:  # noqa: BLE001
                continue


_register_fonts()

INK = colors.HexColor("#111111")
MUTED = colors.HexColor("#666666")
RULE = colors.HexColor("#d9d9d9")
TINT = colors.HexColor("#f4f4f4")


def _eur(cents: int | None) -> str:
    if cents is None:
        return "—"
    v = Decimal(int(cents)) / 100
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def _d(iso: str | None) -> str:
    if not iso:
        return "—"
    d = date.fromisoformat(iso[:10])
    return f"{d.day}.{d.month}.{d.year}"


def _pct(rate: float) -> str:
    s = f"{rate:.1f}".rstrip("0").rstrip(".")
    return s.replace(".", ",") + " %"


def _qty(q) -> str:
    if q is None:
        return "1"
    if float(q) == int(float(q)):
        return str(int(float(q)))
    return f"{float(q):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def virtual_barcode(iban: str | None, total_cents: int, reference: str | None, due_iso: str | None) -> str | None:
    """Finanssiala Pankkiviivakoodi-opas 5.3: numeric v4/v5, 54 digits.
    https://www.finanssiala.fi/wp-content/uploads/2021/03/Pankkiviivakoodi-opas.pdf
    Invalid inputs are unavailable; a large/invalid amount is never silently replaced by zero.
    """
    import re
    from core.refs import fi_reference
    if not iban or not reference or type(total_cents) is not int or not 0 <= total_cents <= 99_999_999:
        return None
    ib = re.sub(r"\s", "", iban).upper()
    if not re.fullmatch(r"FI[0-9]{16}", ib) or int(ib[4:] + "1518" + ib[2:4]) % 97 != 1:
        return None
    ref = re.sub(r"\s", "", reference).upper()
    if ref.startswith("RF"):
        if not re.fullmatch(r"RF[0-9]{3,23}", ref):
            return None
        if int(ref[4:] + "2715" + ref[2:4]) % 97 != 1:
            return None
        version, encoded = "5", ref[2:4] + ref[4:].zfill(21)
    else:
        if not re.fullmatch(r"[1-9][0-9]{3,19}", ref) or fi_reference(ref[:-1]).replace(" ", "") != ref:
            return None
        version, encoded = "4", "000" + ref.zfill(20)
    try:
        d = date.fromisoformat(due_iso[:10]) if due_iso else None
        if d and not 2000 <= d.year <= 2099:
            return None
        due = d.strftime("%y%m%d") if d else "000000"
    except (TypeError, ValueError):
        return None
    return f"{version}{ib[2:]}{total_cents:08d}{encoded}{due}"


def _styles():
    base = ParagraphStyle("b", fontName=_FONT, fontSize=8.5, leading=11, textColor=INK)
    return {
        "base": base,
        "small": ParagraphStyle("s", parent=base, fontSize=7.5, leading=9.5, textColor=MUTED),
        "label": ParagraphStyle("l", parent=base, fontSize=7, leading=9, textColor=MUTED),
        "h1": ParagraphStyle("h1", parent=base, fontName=_FONT, fontSize=16, leading=20),
        "h2": ParagraphStyle("h2", parent=base, fontName=_FONT_B, fontSize=9, leading=12, spaceBefore=4, spaceAfter=3),
        "right": ParagraphStyle("r", parent=base, alignment=TA_RIGHT),
        "bold": ParagraphStyle("bd", parent=base, fontName=_FONT_B),
        "bold_right": ParagraphStyle("bdr", parent=base, fontName=_FONT_B, alignment=TA_RIGHT),
    }


def _esc(s) -> str:
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _addr_lines(addr) -> list[str]:
    if isinstance(addr, str):
        return [addr] if addr else []
    a = addr or {}
    city = " ".join(x for x in [a.get("postal_code"), a.get("city")] if x)
    return [x for x in [a.get("street"), city, a.get("country") if a.get("country") not in (None, "FI") else None] if x]


def _money(cents: int | None, currency: str) -> str:  # s13 multi-currency
    return "—" if cents is None else _eur(cents)[:-1] + currency


def render_invoice_pdf(inv: dict, *, original_number: str | None = None) -> bytes:
    cur = (inv.get("currency") or "EUR").upper()  # s13 multi-currency: amounts in the invoice currency
    _eur = globals()["_eur"] if cur == "EUR" else (lambda c: _money(c, cur))
    st = _styles()
    buf = io.BytesIO()
    is_credit = inv.get("kind") == "credit_note"
    is_draft = inv.get("status") == "draft" or not inv.get("number")
    seller = inv.get("seller") or {}
    buyer = inv.get("buyer") or {}
    pending_fi = "rekisteröinti kesken"
    pending_en = "pending registration"

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(_FONT, 7)
        canvas.setFillColor(MUTED)
        footer = (f"{seller.get('name') or config.COMPANY_NAME} · {seller.get('address') or config.COMPANY_ADDRESS} · "
                  f"Y-tunnus {seller.get('business_id') or pending_fi} · ALV-nro {seller.get('vat_id') or pending_fi}")
        terms = inv.get("terms_version") or "NSAB 2015 (PSYM 2015)"
        rate = _pct(float(inv.get("late_interest_pct", 11.5)))
        canvas.drawString(18 * mm, 16 * mm, "Palvelut PSYM 2015 -ehdoin. Riidat: huolitsijan liikepaikan yleinen tuomioistuin (§ 29).")
        canvas.drawString(18 * mm, 12.5 * mm, "Services under NSAB 2015. Disputes: court at the forwarder's main place of business (§ 29).")
        canvas.drawString(18 * mm, 8 * mm, footer[:150])
        canvas.drawRightString(A4[0] - 18 * mm, 8 * mm, f"{doc.page}")
        if is_draft:
            canvas.setFont(_FONT_B, 60)
            canvas.setFillColor(colors.Color(0.85, 0.85, 0.85, alpha=0.5))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, "LUONNOS / DRAFT")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=22 * mm, title=f"{'Credit note' if is_credit else 'Invoice'} {inv.get('number') or 'draft'}",
                            author=seller.get("name") or config.COMPANY_NAME)
    story = []

    # Header: seller left, document title right.
    title = "HYVITYSLASKU / CREDIT NOTE" if is_credit else "LASKU / INVOICE"
    seller_lines = [f"<font name='{_FONT_B}'>{_esc(seller.get('name') or config.COMPANY_NAME)}</font>",
                    _esc(seller.get("address") or config.COMPANY_ADDRESS),
                    f"Y-tunnus / Business ID: {_esc(seller.get('business_id') or f'{pending_fi} / {pending_en}')}",
                    f"ALV-nro / VAT ID: {_esc(seller.get('vat_id') or f'{pending_fi} / {pending_en}')}"]
    if seller.get("email"):
        seller_lines.append(_esc(seller["email"]) + (f" · {_esc(seller['phone'])}" if seller.get("phone") else ""))
    head = Table([[Paragraph("<br/>".join(seller_lines), st["base"]), Paragraph(title, st["h1"])]],
                 colWidths=[100 * mm, 70 * mm], hAlign="LEFT")
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 6 * mm)]

    # Buyer + invoice facts.
    buyer_lines = [f"<font name='{_FONT_B}'>{_esc(buyer.get('name'))}</font>"] + [_esc(x) for x in _addr_lines(buyer.get("address"))]
    if buyer.get("business_id"):
        buyer_lines.append(f"Y-tunnus: {_esc(buyer['business_id'])}")
    if buyer.get("vat_id"):
        buyer_lines.append(f"ALV-nro / VAT ID: {_esc(buyer['vat_id'])}")
    facts = [
        ("Laskun numero / Invoice no.", inv.get("number") or "LUONNOS / DRAFT"),
        ("Päiväys / Date", _d(inv.get("issue_date"))),
        ("Toimituspäivä / Date of supply", _d(inv.get("supply_date"))),
    ]
    if is_credit:
        facts.append(("Hyvitettävä lasku / Credits invoice", original_number or inv.get("credit_note_for_number") or "—"))
    else:
        facts += [("Eräpäivä / Due date", _d(inv.get("due_date"))),
                  ("Maksuehto / Terms", f"{inv.get('payment_terms_days', 14)} pv netto / days net"),
                  ("Viitenumero / Reference", inv.get("reference") or "—"),
                  ("RF-viite / RF reference", inv.get("rf_reference") or "—")]
    if inv.get("shipment_reference"):
        facts.append(("Lähetys / Shipment", inv["shipment_reference"]))
    if inv.get("customer_reference"):
        facts.append(("Viitteenne / Your reference", inv["customer_reference"]))
    facts.append(("Viivästyskorko / Late interest", _pct(float(inv.get("late_interest_pct", 11.5))) + " p.a."))
    if cur != "EUR":  # s13 multi-currency
        fxd = inv.get("fx") or {}
        facts += [("Valuutta / Currency", cur),
                  ("Kurssi / Exchange rate", f"1 EUR = {fxd.get('rate')} {cur} (EKP / ECB {_d(fxd.get('rate_date'))})")]
    fact_rows = [[Paragraph(_esc(k), st["label"]), Paragraph(_esc(v), st["base"])] for k, v in facts]
    ft = Table(fact_rows, colWidths=[40 * mm, 40 * mm])
    ft.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    top = Table([[Paragraph("<br/>".join(buyer_lines), st["base"]), ft]], colWidths=[90 * mm, 80 * mm], hAlign="LEFT")
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [Paragraph("Laskutettava / Bill to", st["label"]), top, Spacer(1, 5 * mm)]

    lines = inv.get("lines") or []
    services = [ln for ln in lines if not ln.get("is_pass_through")]
    passes = [ln for ln in lines if ln.get("is_pass_through")]

    def line_table(rows, heading):
        data = [[Paragraph(h, st["label"]) for h in ("Kuvaus / Description", "Määrä / Qty", "à-hinta / Unit",
                                                       "ALV / VAT", "Yhteensä / Net")]]
        for ln in rows:
            data.append([Paragraph(_esc(ln.get("description")), st["base"]), Paragraph(_qty(ln.get("qty")), st["right"]),
                         Paragraph(_eur(ln.get("unit_price_cents")), st["right"]),
                         Paragraph(_pct(float(ln.get("vat_rate_pct") or 0)) if ln.get("vat_code") not in (
                             "OUT_OF_SCOPE_DISBURSEMENT", "EU_ICS_RC", "OUT_OF_SCOPE_FOREIGN") else
                                   {"OUT_OF_SCOPE_DISBURSEMENT": "—", "EU_ICS_RC": "RC", "OUT_OF_SCOPE_FOREIGN": "—"}[ln["vat_code"]],
                                   st["right"]),
                         Paragraph(_eur(ln.get("amount_cents")), st["right"])])
        t = Table(data, colWidths=[86 * mm, 16 * mm, 26 * mm, 14 * mm, 28 * mm], repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
        return [Paragraph(heading, st["h2"]), t]

    if services:
        story += line_table(services, "Palvelut / Services")
    if passes:
        story += [Spacer(1, 4 * mm)] + line_table(passes, "Läpikulkuerät (asiakkaan lukuun maksetut) / Disbursements paid on your behalf")
    story.append(Spacer(1, 5 * mm))

    # VAT breakdown + totals side by side.
    vb = [[Paragraph(h, st["label"]) for h in ("ALV-erittely / VAT breakdown", "Veron peruste / Base", "ALV / VAT")]]
    for b in inv.get("vat_breakdown") or []:
        vb.append([Paragraph(_esc(b.get("label_fi")) + " / " + _esc(b.get("label_en")), st["base"]),
                   Paragraph(_eur(b.get("base_cents")), st["right"]), Paragraph(_eur(b.get("vat_cents")), st["right"])])
    vt = Table(vb, colWidths=[54 * mm, 26 * mm, 22 * mm])
    vt.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
    tot_rows = [("Veroton yhteensä / Net total", _eur(inv.get("services_net_cents"))),
                ("ALV yhteensä / VAT total", _eur(inv.get("vat_cents")))]
    if inv.get("pass_through_cents"):
        tot_rows.append(("Läpikulkuerät / Disbursements", _eur(inv.get("pass_through_cents"))))
    tot_rows.append((f"Yhteensä / Total {cur}", _eur(inv.get("total_cents"))))  # s13 multi-currency
    paid = int(inv.get("paid_cents") or 0)
    credited = int(inv.get("credited_cents") or 0)
    if not is_credit and (paid or credited):
        if paid:
            tot_rows.append(("Maksettu / Paid", "−" + _eur(paid)))
        if credited:
            tot_rows.append(("Hyvitetty / Credited", "−" + _eur(credited)))
        tot_rows.append(("Maksettavaa / Amount due", _eur(max(0, int(inv.get("total_cents") or 0) - paid - credited))))
    tt = Table([[Paragraph(_esc(k), st["bold"] if i == len(tot_rows) - 1 else st["base"]),
                 Paragraph(_esc(v), st["bold_right"] if i == len(tot_rows) - 1 else st["right"])]
                for i, (k, v) in enumerate(tot_rows)], colWidths=[42 * mm, 26 * mm])
    tt.setStyle(TableStyle([("LINEABOVE", (0, len(tot_rows) - 1), (-1, len(tot_rows) - 1), 0.6, INK),
                            ("BACKGROUND", (0, len(tot_rows) - 1), (-1, len(tot_rows) - 1), TINT),
                            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    both = Table([[vt, tt]], colWidths=[102 * mm, 68 * mm], hAlign="LEFT")
    both.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [KeepTogether([both]), Spacer(1, 4 * mm)]
    if cur != "EUR" and inv.get("eur"):  # s13 multi-currency: VAT stated in EUR at the ECB rate
        ev, fxd = inv["eur"], inv.get("fx") or {}
        eur_fmt = globals()["_eur"]
        parts = "; ".join(f"{_esc(b.get('vat_code'))} {eur_fmt(b.get('base_cents'))} / ALV {eur_fmt(b.get('vat_cents'))}"
                          for b in ev.get("vat_breakdown") or [])
        story += [Paragraph(f"ALV euroina / VAT in EUR: {eur_fmt(ev.get('vat_cents'))} "
                            f"(veroton / net {eur_fmt(ev.get('subtotal_cents'))}, yhteensä / total {eur_fmt(ev.get('total_cents'))}; "
                            f"EKP:n kurssi / ECB rate 1 EUR = {fxd.get('rate')} {cur}, {_d(fxd.get('rate_date'))}; "
                            f"AVL 73 §, ALV-direktiivi / VAT Directive 2006/112/EC art. 230)."
                            + (f"<br/><font color='#666666'>{parts}</font>" if parts else ""), st["small"]),
                  Spacer(1, 3 * mm)]

    # Legal basis texts (RULE V-4) and import VAT information (RULE V-3).
    notes = []
    for b in inv.get("vat_breakdown") or []:
        lb = b.get("legal_basis")
        if lb:
            notes.append(f"{_esc(lb.get('fi'))}<br/><font color='#666666'>{_esc(lb.get('en'))}</font>")
    for i in inv.get("info") or []:
        if i.get("code") == "IMPORT_VAT_POSTPONED":
            notes.append(f"Maahantuonnin ALV ilmoitetaan arvonlisäveroilmoituksella (MRN {_esc(i.get('mrn'))}).<br/>"
                         "<font color='#666666'>Import VAT: postponed via your VAT return.</font>")
        if i.get("code") == "CUSTOMS_PAID_BY_CUSTOMER":
            notes.append(f"Tullimaksut maksettu suoraan Tullille (MRN {_esc(i.get('mrn'))}).<br/>"
                         "<font color='#666666'>Customs charges paid directly by you to Finnish Customs.</font>")
    if inv.get("notes"):
        notes.append(_esc(inv["notes"]))
    for n in notes:
        story.append(Paragraph(n, st["small"]))
        story.append(Spacer(1, 1.5 * mm))

    # Payment block.
    if not is_credit:
        test_bank = bool(seller.get("test_bank"))  # test tenant: marked TEST account, no IBAN, no virtual barcode
        iban = None if test_bank else seller.get("iban") or config.COMPANY_IBAN
        bic = None if test_bank else seller.get("bic") or config.COMPANY_BIC
        due_amt = max(0, int(inv.get("total_cents") or 0) - paid - credited)
        pay = [
            ("Saajan tilinumero / IBAN", "TESTITILI – ei maksettavissa / TEST ACCOUNT – not payable" if test_bank
             else iban or "Tilitiedot lisätään pian / bank details pending"),
            ("BIC", "TEST" if test_bank else bic or "—"),
            ("Saaja / Payee", seller.get("name") or config.COMPANY_NAME),
            ("Viitenumero / Reference", inv.get("reference") or "—"),
            ("Eräpäivä / Due date", _d(inv.get("due_date"))),
            ("Euro" if cur == "EUR" else cur, _eur(due_amt)),  # s13 multi-currency
        ]
        pt = Table([[Paragraph(_esc(k), st["label"]), Paragraph(_esc(v), st["bold"] if k in ("Euro", cur, "Viitenumero / Reference") else st["base"])]
                    for k, v in pay], colWidths=[44 * mm, 126 * mm], hAlign="LEFT")
        pt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -2), 0.3, RULE),
                                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 3),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        block = [Spacer(1, 2 * mm), Paragraph("Tilisiirto / Bank transfer", st["h2"]), pt]
        vbc = virtual_barcode(iban, due_amt, inv.get("reference"), inv.get("due_date")) if not is_draft and cur == "EUR" else None
        if vbc:
            bc = Code128(vbc, barHeight=11 * mm, barWidth=0.25 * mm, humanReadable=False, quiet=False)
            bc.hAlign = "LEFT"
            block += [Spacer(1, 3 * mm), bc, Paragraph(f"Virtuaaliviivakoodi / Virtual barcode: {vbc}", st["small"])]
        story.append(KeepTogether(block))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
