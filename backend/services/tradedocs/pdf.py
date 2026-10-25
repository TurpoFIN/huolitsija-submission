"""PDF renderers for the trade documents (reportlab), one A4 layout per kind, Finnish or English.

render(kind, ctx, lang="fi"|"en", meta={doc_no, version, status, issued_at, amendment, first_issued_at}) -> PDF bytes

Layouts follow the forms the trade knows, so a carrier, terminal or customs officer finds each field where they expect it:
- house B/L: the FIATA / BIMCO bill of lading field order (shipper, consignee, notify, vessel/voyage, ports, particulars
  furnished by the shipper, freight, number of originals, place and date of issue, shipped on board).
- HAWB: the IATA air waybill layout (Resolution 600a): shipper / consignee / issuing carrier's agent, airport of departure
  and routing, currency, charge codes, declared values, rating block (pieces, gross weight, rate class, chargeable weight,
  rate, total, nature and quantity of goods), prepaid / collect summary, shipper's certification, carrier execution.
- CMR: the 24 numbered boxes of the IRU model consignment note (CMR Convention art. 6).
- EUR.1: boxes 1-12 of the movement certificate and the exporter's application (Regulation (EU) 2015/2447 annex 22-10).
- certificate of origin: the ICC / Finland Chamber of Commerce application boxes 1-7.
- DG: the IMO Multimodal Dangerous Goods Form (IMDG 5.4.5) for sea / road / rail, the IATA Shipper's Declaration for
  Dangerous Goods (DGR 8.1) for air.
"""
from __future__ import annotations

import io
from datetime import date
from xml.sax.saxutils import escape

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.barcode.code128 import Code128
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from core import config
from core.util import flight_label
from services import invoice_pdf as _fonts  # registers the HuoSans TrueType fonts (Finnish characters)
from services.tradedocs import catalog

FONT, FONT_B = _fonts._FONT, _fonts._FONT_B
INK = colors.HexColor("#111111")
MUTED = colors.HexColor("#5f5f5f")
RULE = colors.HexColor("#9a9a9a")
TINT = colors.HexColor("#f3f3f3")
W, H = A4
M = 10 * mm  # page margin

MODE = {"sea": ("Meri", "Sea"), "air": ("Lento", "Air"), "road": ("Maantie", "Road"), "rail": ("Rautatie", "Rail")}


# ------------------------------------------------------------------------------------------------ formatting
def t(lang: str, fi: str, en: str) -> str:
    return fi if lang == "fi" else en


def d(iso: str | None, lang: str) -> str:
    if not iso:
        return ""
    try:
        x = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    return f"{x.day}.{x.month}.{x.year}" if lang == "fi" else x.strftime("%d %b %Y")


def n(v, lang: str, dec: int = 0) -> str:
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{f:,.{dec}f}"
    if lang == "fi":
        s = s.replace(",", " ").replace(".", ",")
    return s


def kg(v, lang):
    return f"{n(v, lang, 1 if v and float(v) < 100 else 0)} kg" if v not in (None, "") else ""


def money(v, cur, lang):
    if v in (None, ""):
        return ""
    return f"{n(v, lang, 2)} {cur or ''}".strip()


def party(p: dict | None, lang: str, *, ids: bool = True) -> str:
    if not p:
        return ""
    a = p.get("address") or {}
    lines = [p.get("name") or "", a.get("street") or "", " ".join(x for x in (a.get("postal_code"), a.get("city")) if x),
             a.get("country") or ""]
    if p.get("contact_name"):
        lines.append(f"{t(lang, 'Yhteyshenkilö', 'Contact')}: {p['contact_name']}")
    extra = []
    if ids and p.get("eori"):
        extra.append(f"EORI {p['eori']}")
    if ids and p.get("vat_id"):
        extra.append(f"{t(lang, 'ALV', 'VAT')} {p['vat_id']}")
    if extra:
        lines.append(" · ".join(extra))
    return "\n".join(x for x in lines if x)


def place(p: dict | None) -> str:
    if not p:
        return ""
    name = p.get("name") or ""
    code = p.get("unlocode")
    cc = p.get("country")
    out = name
    if code and code not in name:
        out += f" ({code})"
    elif cc and cc not in name:
        out += f", {cc}"
    return out


def pkg_text(g: dict, lang) -> str:
    if not g.get("packages"):
        return ""
    return f"{n(g['packages'], lang)} {g.get('package_type') or ''}".strip()


# ------------------------------------------------------------------------------------------------ the form engine
class Sheet:
    """A4 sheet with mm coordinates measured from the top-left corner."""

    def __init__(self, c: canvas.Canvas, lang: str, kind: str, ctx: dict, meta: dict):
        self.c, self.lang, self.kind, self.ctx, self.meta = c, lang, kind, ctx, meta
        self.page = 0

    # y measured from the top edge in mm
    def Y(self, y_mm: float) -> float:
        return H - y_mm * mm

    def para(self, size: float = 8.5, bold: bool = False, color=INK, leading: float | None = None) -> ParagraphStyle:
        return ParagraphStyle("p", fontName=FONT_B if bold else FONT, fontSize=size, leading=leading or size * 1.22,
                              textColor=color)

    def text_in(self, x, y, w, h, text: str, size=8.5, bold=False, top_pad=3.6):
        """Wrap `text` inside the box; shrink the font until it fits."""
        if not text:
            return
        body = escape(str(text)).replace("\n", "<br/>")
        s = size
        while True:
            p = Paragraph(body, self.para(s, bold))
            pw, ph = p.wrap((w - 2.4) * mm, (h - top_pad - 1) * mm)
            if ph <= (h - top_pad - 0.8) * mm or s <= 5.5:
                c = self.c
                c.saveState()
                clip = c.beginPath()
                clip.rect(x * mm, self.Y(y + h), w * mm, (h - top_pad + 2.6) * mm)
                c.clipPath(clip, stroke=0, fill=0)
                p.drawOn(c, (x + 1.2) * mm, self.Y(y + top_pad) - ph)
                c.restoreState()
                return
            s -= 0.5

    def box(self, x, y, w, h, label: str = "", value: str = "", *, num: str | None = None, size=8.5, bold=False, fill=None):
        c = self.c
        c.setStrokeColor(RULE)
        c.setLineWidth(0.5)
        if fill:
            c.setFillColor(fill)
            c.rect(x * mm, self.Y(y + h), w * mm, h * mm, stroke=1, fill=1)
        else:
            c.rect(x * mm, self.Y(y + h), w * mm, h * mm, stroke=1, fill=0)
        if label:
            c.setFillColor(MUTED)
            c.setFont(FONT, 5.8)
            c.drawString((x + 1.2) * mm, self.Y(y + 2.4), (f"{num}  " if num else "") + label)
        c.setFillColor(INK)
        self.text_in(x, y, w, h, value, size=size, bold=bold, top_pad=3.6 if label else 1.4)

    def table(self, x, y, w, h, header: list[str], rows: list[list], widths: list[float], *, label: str = "",
              num: str | None = None, align_right: set[int] | None = None, size=7.6) -> list[list]:
        """Draw as many rows as fit in the box; returns the rows that did not fit (for the continuation sheet)."""
        self.box(x, y, w, h, label, num=num)
        top = 3.4 if label else 0.6
        style = TableStyle([("FONT", (0, 0), (-1, -1), FONT, size), ("FONT", (0, 0), (-1, 0), FONT_B, size - 0.4),
                            ("TEXTCOLOR", (0, 0), (-1, 0), MUTED), ("LINEBELOW", (0, 0), (-1, 0), 0.4, RULE),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2.2),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 2.2), ("TOPPADDING", (0, 0), (-1, -1), 1.4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.4)])
        for col in align_right or set():
            style.add("ALIGN", (col, 0), (col, -1), "RIGHT")
        cell = self.para(size)
        head_style = ParagraphStyle("h", fontName=FONT_B, fontSize=size - 0.6, leading=(size - 0.6) * 1.15, textColor=MUTED)
        head = [Paragraph(escape(str(hd)), head_style) for hd in header]
        body = [[Paragraph(escape(str(v if v is not None else "")).replace("\n", "<br/>"), cell) for v in r] for r in rows]
        fit = len(body)
        while fit >= 0:
            tb = Table([head] + body[:fit], colWidths=[cw * mm for cw in widths], style=style, repeatRows=1)
            tw, th = tb.wrap(w * mm, (h - top) * mm)
            if th <= (h - top - 0.5) * mm:
                tb.drawOn(self.c, x * mm, self.Y(y + top) - th)
                break
            fit -= 1
        return rows[max(fit, 0):]

    # ---- page furniture
    def header(self, title: str, subtitle: str = "", *, top: float = 10, show_issuer: bool = True) -> float:
        c, lang, meta, iss = self.c, self.lang, self.meta, self.ctx["issuer"]
        self.page += 1
        if show_issuer:
            c.setFillColor(INK)
            c.setFont(FONT_B, 10.5)
            c.drawString(M, self.Y(top + 4), iss.get("name") or config.COMPANY_NAME)
            c.setFont(FONT, 7)
            c.setFillColor(MUTED)
            a = iss.get("address") or {}
            line = ", ".join(x for x in (a.get("street"), " ".join(y for y in (a.get("postal_code"), a.get("city")) if y), "Finland") if x)
            c.drawString(M, self.Y(top + 8), line)
            ids = " · ".join(x for x in (f"Y-tunnus {iss['business_id']}" if iss.get("business_id") else "",
                                         f"EORI {iss['eori']}" if iss.get("eori") else "", iss.get("email") or "") if x)
            c.drawString(M, self.Y(top + 11.5), ids)
        c.setFillColor(INK)
        c.setFont(FONT_B, 13)
        c.drawRightString(W - M - 42 * mm, self.Y(top + 4.5), title)
        c.setFont(FONT, 7.5)
        c.setFillColor(MUTED)
        if subtitle:
            c.drawRightString(W - M - 42 * mm, self.Y(top + 8.5), subtitle)
        ver = t(lang, "Versio", "Version") + f" {meta['version']}"
        if meta.get("amendment"):
            ver += " · " + t(lang, "Korjaus", "Amendment") + f" {meta['amendment']['number']}"
        c.drawRightString(W - M - 42 * mm, self.Y(top + 12), ver)
        # number + barcode block
        c.setFillColor(INK)
        c.setFont(FONT_B, 11)
        c.drawRightString(W - M, self.Y(top + 4.5), meta["doc_no"])
        bc = Code128(meta["doc_no"], barHeight=7 * mm, barWidth=0.62)
        # Long document numbers must stay in their reserved column, clear of the version/subtitle.
        barcode_width = min(bc.width, 40 * mm)
        c.saveState()
        c.translate(W - M - barcode_width, self.Y(top + 14))
        c.scale(barcode_width / bc.width, 1)
        bc.drawOn(c, 0, 0)
        c.restoreState()
        if meta.get("status") == "draft":
            self.watermark(t(lang, "LUONNOS", "DRAFT"))
        if self.ctx["shipment"].get("demo"):
            c.setFont(FONT, 6.5)
            c.setFillColor(colors.HexColor("#a33"))
            c.drawString(M, self.Y(top + 15), t(lang, "DEMO — esittelylähetyksen tiedoista, ei todellinen kauppa",
                                                  "DEMO — from a demonstration shipment, not a real transaction"))
            c.setFillColor(INK)
        return top + 17

    def watermark(self, word: str):
        c = self.c
        c.saveState()
        c.setFillColor(colors.Color(0.75, 0.75, 0.75, alpha=0.28))
        c.setFont(FONT_B, 92)
        c.translate(W / 2, H / 2)
        c.rotate(35)
        c.drawCentredString(0, 0, word)
        c.restoreState()

    def footer(self):
        c, lang, meta, iss = self.c, self.lang, self.meta, self.ctx["issuer"]
        c.setStrokeColor(RULE)
        c.line(M, 14 * mm, W - M, 14 * mm)
        c.setFont(FONT, 6.3)
        c.setFillColor(MUTED)
        left = f"{iss.get('name')} · {self.ctx['shipment']['reference']} · {meta['doc_no']} v{meta['version']}"
        c.drawString(M, 10.5 * mm, left)
        issued = t(lang, "Laadittu", "Issued") + f" {d(meta.get('issued_at'), lang)}"
        if meta.get("amendment"):
            am = meta["amendment"]
            issued += " · " + t(lang, "Korjattu", "Amended") + f": {am.get('reason', '')}"[:110]
        c.drawString(M, 7.5 * mm, issued)
        c.drawRightString(W - M - 14 * mm, 10.5 * mm, t(lang, "Sivu", "Page") + f" {self.page}")
        url = f"{(config.APP_BASE_URL or 'https://huolitsija.fi').rstrip('/')}/api/trade-documents/verify/{meta['doc_no']}?v={meta['version']}"
        q = qr.QrCodeWidget(url)
        b = q.getBounds()
        size = 12 * mm
        dr = Drawing(size, size, transform=[size / (b[2] - b[0]), 0, 0, size / (b[3] - b[1]), 0, 0])
        dr.add(q)
        renderPDF.draw(dr, c, W - M - size, 2 * mm)
        c.setFillColor(INK)

    def clause(self, x, y, w, h, text: str, size=6.2):
        self.c.setFillColor(MUTED)
        p = Paragraph(escape(text).replace("\n", "<br/>"), self.para(size, color=MUTED))
        _pw, ph = p.wrap(w * mm, h * mm)
        p.drawOn(self.c, x * mm, self.Y(y) - ph)
        self.c.setFillColor(INK)

    def sign(self, x, y, w, h, label: str, name: str, when: str = ""):
        self.box(x, y, w, h, label)
        self.c.setFont(FONT, 7.5)
        self.c.drawString((x + 1.5) * mm, self.Y(y + h - 5), name)
        if when:
            self.c.drawString((x + 1.5) * mm, self.Y(y + h - 1.8), when)
        self.c.setFont(FONT, 6.2)
        self.c.setFillColor(MUTED)
        self.c.drawRightString((x + w - 1.5) * mm, self.Y(y + h - 1.8),
                               t(self.lang, "Järjestelmän laatima · SHA-256-tarkiste", "System generated · SHA-256 checksum"))
        self.c.setFillColor(INK)

    def next_page(self, title: str):
        self.footer()
        self.c.showPage()
        return self.header(title, t(self.lang, "Jatkolehti", "Continuation sheet"))

    def rows_overflow(self, title: str, header: list[str], rows: list[list], widths: list[float], right: set[int] | None = None):
        while rows:
            top = self.next_page(title)
            rows = self.table(M / mm, top + 2, (W - 2 * M) / mm, 250 - top, header, rows, widths, align_right=right)


# ------------------------------------------------------------------------------------------------ shared pieces
def goods_rows(ctx, lang, *, with_value=False, with_hs=True, with_marks=True):
    rows = []
    for g in ctx["goods"]:
        r = []
        if with_marks:
            r.append(g.get("marks") or (ctx["refs"].get("shipment") if g["seq"] == 1 else ""))
        r += [pkg_text(g, lang), g.get("description") or ""]
        if with_hs:
            r.append(g.get("hs_code") or "")
        r.append(kg(g.get("gross_kg"), lang))
        if with_value:
            r.append(money(g.get("amount"), g.get("currency"), lang))
        rows.append(r)
    return rows


def containers_text(ctx, lang) -> str:
    cs = ctx.get("containers") or []
    if not cs:
        return ""
    return "\n".join(f"{c['number']} · {c.get('type') or ''}" + (f" · {t(lang, 'sinetti', 'seal')} {c['seal']}" if c.get("seal") else "")
                     for c in cs)


def freight_terms(ctx, lang) -> str:
    f = ctx["freight"]
    return t(lang, "Rahti maksettu (prepaid)", "Freight prepaid") if f["terms"] == "prepaid" else t(lang, "Rahti maksetaan perillä (collect)", "Freight collect")


def vessel_voyage(ctx) -> str:
    m = ctx["main_leg"]
    v = m.get("vessel") or ""
    if m.get("voyage"):
        v += f" / {m['voyage']}"
    if m.get("imo"):
        v += f" (IMO {m['imo']})"
    return v or (m.get("carrier") or "")


# ------------------------------------------------------------------------------------------------ the documents
def _house_bl(s: Sheet):
    ctx, lang = s.ctx, s.lang
    neg = bool(ctx.get("negotiable"))
    master = s.kind == "master_bl"
    title = t(lang, "MASTER B/L — LUONNOS", "MASTER BILL OF LADING — DRAFT") if master else t(lang, "KONOSSEMENTTI", "BILL OF LADING")
    sub = (t(lang, "Huolitsijan konossementti · siirtokelpoinen", "House B/L · negotiable") if neg else
           t(lang, "Huolitsijan konossementti · ei siirtokelpoinen (merirahtikirja)", "House B/L · non-negotiable (sea waybill)"))
    if master:
        sub = t(lang, "Tarkistettavaksi rahdinkuljettajalle — ei myönnetty konossementti", "For carrier review — not a carrier-issued bill of lading")
    y = s.header(title, sub)
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 27, t(lang, "Lähettäjä (shipper)", "Shipper"), party(ctx["shipper"], lang))
    s.box(X + half, y, half / 2, 9, t(lang, "Konossementin numero", "B/L number"), (ctx["refs"].get("mbl") or t(lang, "Rahdinkuljettaja ei ole antanut numeroa", "Carrier reference not assigned")) if master else s.meta["doc_no"], bold=True)
    s.box(X + half * 1.5, y, half / 2, 9, t(lang, "Varausnumero", "Booking reference"), ctx["refs"].get("booking") or ctx["shipment"]["reference"])
    s.box(X + half, y + 9, half, 9, t(lang, "Viitteet", "Export references"),
          " · ".join(x for x in [ctx["shipment"]["reference"], ctx["refs"].get("customer") or "", ", ".join(ctx["refs"].get("po") or [])] if x))
    s.box(X + half, y + 18, half, 9, (t(lang, "Valtamerirahdinkuljettaja", "Ocean carrier") if master else t(lang, "Rahdinkuljettaja (huolitsija sopimusrahdinkuljettajana)", "Carrier (forwarder as contracting carrier)")),
          (ctx["main_leg"].get("carrier") or t(lang, "Rahdinkuljettaja puuttuu", "Carrier missing")) if master else ctx["issuer"]["name"] + (f" · SCAC {ctx['issuer']['scac']}" if ctx["issuer"].get("scac") else ""))
    y += 27
    s.box(X, y, half, 25, t(lang, "Vastaanottaja (consignee)", "Consignee") + (t(lang, " — määräyksestä", " — to order") if neg else ""),
          party(ctx["consignee"], lang))
    s.box(X + half, y, half, 25, t(lang, "Ilmoitettava osapuoli (notify)", "Notify party"),
          party(ctx["notify"], lang) or t(lang, "Sama kuin vastaanottaja", "Same as consignee"))
    y += 25
    q = Wd / 4
    pre = next((lg for lg in ctx["legs"] if lg["mode"] != "sea" and lg["seq"] < (ctx["main_leg"].get("seq") or 0)), None)
    s.box(X, y, q, 10, t(lang, "Esikuljetus", "Pre-carriage by"), (pre or {}).get("carrier") or (MODE.get((pre or {}).get("mode"), ("", ""))[0 if lang == "fi" else 1] if pre else ""))
    s.box(X + q, y, q, 10, t(lang, "Vastaanottopaikka", "Place of receipt"), place(ctx["place_of_receipt"]))
    s.box(X + 2 * q, y, q, 10, t(lang, "Alus / matka", "Vessel / voyage"), vessel_voyage(ctx))
    s.box(X + 3 * q, y, q, 10, t(lang, "Lastaussatama", "Port of loading"), place(ctx["port_of_loading"]))
    y += 10
    s.box(X, y, q, 10, t(lang, "Purkaussatama", "Port of discharge"), place(ctx["port_of_discharge"]))
    s.box(X + q, y, q, 10, t(lang, "Määräpaikka", "Place of delivery"), place(ctx["place_of_delivery"]))
    s.box(X + 2 * q, y, q, 10, t(lang, "Valtamerirahdinkuljettaja", "Ocean carrier"), ctx["main_leg"].get("carrier") or "")
    s.box(X + 2 * q + q, y, q, 10, t(lang, "Toimitusehto", "Incoterms® 2020"),
          f"{ctx['shipment'].get('incoterm') or ''} {ctx['shipment'].get('incoterm_place') or ''}".strip())
    y += 10
    s.c.setFont(FONT_B, 7)
    s.c.drawString(X * mm + 2, s.Y(y + 3.2), t(lang, "LÄHETTÄJÄN ILMOITTAMAT TIEDOT — rahdinkuljettaja ei tunne sisältöä",
                                                "PARTICULARS FURNISHED BY THE SHIPPER — carrier not responsible"))
    y += 4.5
    hdr = [t(lang, "Merkit / kontti, sinetti", "Marks / container, seal"), t(lang, "Kollit", "Packages"),
           t(lang, "Tavaran kuvaus", "Description of goods"), t(lang, "HS", "HS code"), t(lang, "Bruttopaino", "Gross weight")]
    rows = goods_rows(s.ctx, lang)
    if rows:
        rows[0][0] = "\n".join(x for x in [rows[0][0], containers_text(ctx, lang)] if x)
    tot = ctx["totals"]
    rows.append(["", f"{n(tot.get('packages'), lang)} {tot.get('package_type') or ''}", t(lang, "Yhteensä", "Total") +
                 (f" · {n(tot.get('volume_m3'), lang, 1)} m³" if tot.get("volume_m3") else ""), "", kg(tot.get("gross_kg"), lang)])
    if ctx["dangerous_goods"]:
        for g in ctx["dangerous_goods"]:
            rows.append(["", "", f"{g['un_number']} {g.get('proper_shipping_name') or ''}, {t(lang, 'luokka', 'class')} {g['class']}"
                                 + (f", PG {g['packing_group']}" if g.get("packing_group") else ""), "", ""])
    widths = [42, 24, 78, 18, Wd - 162]
    rest = s.table(X, y, Wd, 76, hdr, rows, widths, align_right={4})
    y += 76
    f = ctx["freight"]
    s.box(X, y, Wd * 0.38, 15, t(lang, "Rahti ja maksut", "Freight and charges"),
          freight_terms(ctx, lang) + "\n" + t(lang, "Sopimuksen mukaan", "As arranged"))
    s.box(X + Wd * 0.38, y, Wd * 0.22, 15, t(lang, "Rahti maksetaan", "Freight payable at"), f.get("payable_at") or "")
    s.box(X + Wd * 0.6, y, Wd * 0.2, 15, t(lang, "Alkuperäiskappaleita", "Number of originals"),
          t(lang, "3 (KOLME)", "3 (THREE)") if neg else t(lang, "0 — merirahtikirja", "0 — sea waybill"))
    s.box(X + Wd * 0.8, y, Wd * 0.2, 15, t(lang, "Laivattu (shipped on board)", "Shipped on board"),
          d(ctx["dates"].get("shipped_on_board"), lang) or t(lang, "vastaanotettu kuljetettavaksi", "received for shipment"))
    y += 15
    s.box(X, y, Wd * 0.4, 14, t(lang, "Laatimispaikka ja -päivä", "Place and date of issue"),
          f"{ctx['issuer']['address'].get('city') or 'Helsinki'}, {d(ctx['dates']['issue'], lang)}")
    if master:
        s.box(X + Wd * 0.4, y, Wd * 0.6, 14, t(lang, "Rahdinkuljettajan hyväksyntä", "Carrier acceptance"),
              t(lang, "Odottaa rahdinkuljettajan myöntämää asiakirjaa", "Awaiting carrier-issued document"))
    else:
        s.sign(X + Wd * 0.4, y, Wd * 0.6, 14, t(lang, "Allekirjoitus rahdinkuljettajana", "Signed as carrier"),
               f"{ctx['issuer']['name']}, {t(lang, 'sopimusrahdinkuljettajana', 'as carrier')}")
    y += 16
    s.clause(X, y, Wd, 30, t(lang,
        "Vastaanotettu näennäisesti hyvässä kunnossa, ellei toisin mainita, kuljetettavaksi yllä mainitulta vastaanottopaikalta "
        "määräpaikkaan. Kuljetukseen sovelletaan Haagin–Visbyn sääntöjä (merilaki 13 luku) sekä NSAB 2015 -yleisiä määräyksiä. "
        "Siirtokelpoista konossementtia vastaan tavara luovutetaan vain yhtä alkuperäiskappaletta vastaan; kun yksi on käytetty, muut "
        "ovat mitättömiä. Merirahtikirjaa vastaan tavara luovutetaan nimetylle vastaanottajalle henkilöllisyyden toteamisen jälkeen.",
        "Received in apparent good order and condition unless otherwise stated, for carriage from the place of receipt to the place "
        "of delivery shown above, subject to the Hague-Visby Rules (Finnish Maritime Code ch. 13) and NSAB 2015. For a negotiable B/L "
        "one original duly endorsed must be surrendered in exchange for the goods; one being accomplished, the others stand void. "
        "Under a sea waybill the goods are delivered to the named consignee on proof of identity."))
    s.footer()
    if rest:
        s.rows_overflow(title, hdr, rest, widths, {4})


def _hawb(s: Sheet):
    ctx, lang = s.ctx, s.lang
    title = t(lang, "LENTORAHTIKIRJA (HAWB)", "HOUSE AIR WAYBILL")
    y = s.header(title, t(lang, "Ei siirtokelpoinen · IATA-lomakemalli", "Not negotiable · IATA layout"))
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    m = ctx["main_leg"]
    mawb = ctx["refs"].get("awb_master") or ""
    s.box(X, y, half, 24, t(lang, "Lähettäjän nimi ja osoite", "Shipper's name and address"), party(ctx["shipper"], lang))
    s.box(X + half, y, half / 2, 8, t(lang, "HAWB-numero", "HAWB number"), s.meta["doc_no"], bold=True)
    s.box(X + half * 1.5, y, half / 2, 8, t(lang, "Master-AWB", "Master AWB"), mawb)
    s.box(X + half, y + 8, half, 16, t(lang, "Rahtikirjan antaja", "Issued by"), ctx["issuer"]["name"] + "\n" + t(lang,
          "Kopiot 1, 2 ja 3 ovat alkuperäiskappaleita ja yhtä pätevät.", "Copies 1, 2 and 3 of this air waybill are originals and have the same validity."))
    y += 24
    s.box(X, y, half, 22, t(lang, "Vastaanottajan nimi ja osoite", "Consignee's name and address"), party(ctx["consignee"], lang))
    s.box(X + half, y, half, 22, t(lang, "Kuljetusehdot", "Conditions of contract"), t(lang,
          "Tavara vastaanotettu kuljetettavaksi näennäisesti hyvässä kunnossa. Kuljetukseen sovelletaan Montrealin yleissopimusta "
          "(1999) ja rahtikirjan kääntöpuolen ehtoja.", "It is agreed that the goods described herein are accepted in apparent good "
          "order and condition for carriage subject to the conditions of contract; the Montreal Convention 1999 applies."), size=6.8)
    y += 22
    s.box(X, y, half, 13, t(lang, "Rahtikirjan antajan agentti, nimi ja kaupunki", "Issuing carrier's agent name and city"),
          f"{ctx['issuer']['name']}, {ctx['issuer']['address'].get('city') or 'Helsinki'}")
    s.box(X + half, y, half, 13, t(lang, "Tilitystiedot", "Accounting information"),
          f"{freight_terms(ctx, lang)} · {ctx['shipment']['reference']}")
    y += 13
    s.box(X, y, half / 2, 9, t(lang, "Agentin IATA-koodi", "Agent's IATA code"), ctx["issuer"].get("iata_agent_code") or "")
    s.box(X + half / 2, y, half / 2, 9, t(lang, "Tilinumero", "Account no."), "")
    s.box(X + half, y, half, 9, t(lang, "Lähtölentoasema ja reititys", "Airport of departure and requested routing"),
          place(ctx["port_of_loading"]))
    y += 9
    air = [lg for lg in ctx["legs"] if lg["mode"] == "air"]
    route = " – ".join([air[0]["from"].get("iata") or place(air[0]["from"])] + [lg["to"].get("iata") or place(lg["to"]) for lg in air]) if air else ""
    s.box(X, y, Wd * 0.5, 9, t(lang, "Reitti ja lentoyhtiöt", "Routing and carriers"), route + ("\n" + " / ".join(dict.fromkeys(lg.get("carrier") or "" for lg in air)) if air else ""), size=7.2)
    s.box(X + Wd * 0.5, y, Wd * 0.08, 9, t(lang, "Valuutta", "Currency"), ctx["freight"]["currency"])
    pp = ctx["freight"]["terms"] == "prepaid"
    s.box(X + Wd * 0.58, y, Wd * 0.1, 9, "WT/VAL", "PPD" if pp else "COLL")
    s.box(X + Wd * 0.68, y, Wd * 0.16, 9, t(lang, "Ilmoitettu arvo kuljetukseen", "Declared value for carriage"), "NVD")
    s.box(X + Wd * 0.84, y, Wd * 0.16, 9, t(lang, "Ilmoitettu arvo tulliin", "Declared value for customs"),
          money(ctx["totals"].get("amount"), ctx["totals"].get("currency"), lang) or "NCV")
    y += 9
    s.box(X, y, Wd * 0.3, 9, t(lang, "Määräasema", "Airport of destination"), place(ctx["port_of_discharge"]))
    s.box(X + Wd * 0.3, y, Wd * 0.3, 9, t(lang, "Lento / päivä", "Requested flight / date"),
          " ".join(x for x in [flight_label(m.get("flight")), d(m.get("etd"), lang)] if x))
    ins = ctx.get("insurance") or {}
    s.box(X + Wd * 0.6, y, Wd * 0.4, 9, t(lang, "Vakuutusmäärä", "Amount of insurance"),
          (f"{ins.get('type')} {ins.get('insured_value_pct') or ''} %" if ins.get("type") else "NIL"))
    y += 9
    s.box(X, y, Wd, 12, t(lang, "Käsittelyohjeet", "Handling information"),
          ("; ".join(f"{g['un_number']} {g.get('proper_shipping_name') or ''} — " + t(lang, "katso vaarallisten aineiden ilmoitus", "Dangerous goods as per attached Shipper's Declaration") for g in ctx["dangerous_goods"]))
          or t(lang, "Ilmoita vastaanottajalle saapumisesta.", "Notify consignee on arrival."))
    y += 12
    hdr = [t(lang, "Kollit RCP", "No. of pieces RCP"), t(lang, "Bruttopaino", "Gross weight"), "kg/lb", t(lang, "Hintaluokka", "Rate class"),
           t(lang, "Rahdituspaino", "Chargeable weight"), t(lang, "Hinta / maksu", "Rate / charge"), t(lang, "Yhteensä", "Total"),
           t(lang, "Tavaran laji ja määrä (mitat tai tilavuus)", "Nature and quantity of goods (incl. dimensions or volume)")]
    tot = ctx["totals"]
    desc = "\n".join(g.get("description") or "" for g in ctx["goods"])
    if tot.get("volume_m3"):
        desc += f"\n{t(lang, 'Tilavuus', 'Volume')} {n(tot['volume_m3'], lang, 2)} m³"
    if ctx["goods"] and any(g.get("hs_code") for g in ctx["goods"]):
        desc += "\nHS " + ", ".join(dict.fromkeys(g["hs_code"] for g in ctx["goods"] if g.get("hs_code")))
    total_amt = (ctx["freight"].get("total_cents") or 0) / 100 or None
    rows = [[n(tot.get("packages"), lang), n(tot.get("gross_kg"), lang, 1), "K", "Q", n(tot.get("chargeable_kg"), lang, 1),
             t(lang, "sopimuksen mukaan", "as agreed"), money(total_amt, ctx["freight"]["currency"], lang) if total_amt else t(lang, "sop. mukaan", "as agreed"), desc]]
    widths = [17, 20, 10, 16, 21, 22, 24, Wd - 130]
    s.table(X, y, Wd, 52, hdr, rows, widths, align_right={0, 1, 4, 6})
    y += 52
    q = Wd / 4
    s.box(X, y, q, 10, t(lang, "Ennakkoon maksettu rahti", "Prepaid weight charge"), money(total_amt, ctx["freight"]["currency"], lang) if pp and total_amt else "")
    s.box(X + q, y, q, 10, t(lang, "Jälkikäteen maksettava rahti", "Collect weight charge"), money(total_amt, ctx["freight"]["currency"], lang) if not pp and total_amt else "")
    s.box(X + 2 * q, y, 2 * q, 10, t(lang, "Muut maksut", "Other charges"), t(lang, "sopimuksen mukaan", "as agreed"))
    y += 10
    s.box(X, y, Wd, 14, t(lang, "Lähettäjän vakuutus", "Shipper's certification"), t(lang,
          "Lähettäjä vakuuttaa, että tiedot ovat oikein ja että lähetyksen vaaralliset aineet on nimetty ja pakattu IATA:n "
          "vaarallisten aineiden määräysten mukaisesti.", "Shipper certifies that the particulars on the face hereof are correct and "
          "that insofar as any part of the consignment contains dangerous goods, such part is properly described by name and is in "
          "proper condition for carriage by air according to the applicable Dangerous Goods Regulations.") + f"\n{(ctx['shipper'] or {}).get('name') or ''}", size=7)
    y += 14
    s.sign(X, y, Wd, 13, t(lang, "Laadittu (päivä, paikka) ja rahtikirjan antajan tai agentin allekirjoitus",
                           "Executed on (date) at (place) — signature of issuing carrier or its agent"),
           f"{ctx['issuer']['name']}", f"{d(ctx['dates']['issue'], lang)}, {ctx['issuer']['address'].get('city') or 'Helsinki'}")
    s.footer()


def _cmr(s: Sheet):
    ctx, lang = s.ctx, s.lang
    leg = dict((ctx["road_legs_international"] or [lg for lg in ctx["legs"] if lg["mode"] == "road"] or [ctx["main_leg"]])[0])
    if ctx["shipment"]["mode"] == "road" and ctx["legs"]:
        # One CMR covers the whole road carriage, ro-ro ferry legs included (CMR art. 2).
        leg["from"], leg["to"] = ctx["legs"][0]["from"], ctx["legs"][-1]["to"]
    title = "CMR"
    y = s.header(t(lang, "KANSAINVÄLINEN RAHTIKIRJA", "INTERNATIONAL CONSIGNMENT NOTE"), "CMR · " + t(lang,
                 "Tiekuljetussopimusyleissopimus (Geneve 1956)", "Convention on the Contract for the International Carriage of Goods by Road (Geneva 1956)"))
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    frm, to = leg.get("from") or {}, leg.get("to") or {}
    s.box(X, y, half, 22, t(lang, "Lähettäjä (nimi, osoite, maa)", "Sender (name, address, country)"), party(ctx["shipper"], lang), num="1")
    s.box(X + half, y, half, 22, t(lang, "Rahdinkuljettaja (nimi, osoite, maa)", "Carrier (name, address, country)"),
          "\n".join(x for x in [leg.get("carrier") or "", t(lang, "Rekisterinumero", "Registration no.") + f": {leg.get('truck_plate')}" if leg.get("truck_plate") else ""] if x), num="16")
    y += 22
    s.box(X, y, half, 22, t(lang, "Vastaanottaja (nimi, osoite, maa)", "Consignee (name, address, country)"), party(ctx["consignee"], lang), num="2")
    s.box(X + half, y, half, 22, t(lang, "Seuraavat rahdinkuljettajat", "Successive carriers"),
          "\n".join(f"{lg.get('carrier') or ''} ({place(lg['from'])} – {place(lg['to'])})" for lg in ctx["legs"]
                    if lg is not leg and lg["mode"] == "road" and lg.get("carrier"))[:300], num="17")
    y += 22
    dest = (to.get("address") or {})
    s.box(X, y, half, 13, t(lang, "Tavaran luovutuspaikka (paikka, maa)", "Place of delivery of the goods (place, country)"),
          "\n".join(x for x in [place(to), " ".join(y for y in (dest.get("street"), dest.get("postal_code"), dest.get("city")) if y)] if x), num="3")
    s.box(X + half, y, half, 26, t(lang, "Rahdinkuljettajan varaumat ja huomautukset", "Carrier's reservations and observations"), "", num="18")
    y += 13
    s.box(X, y, half, 13, t(lang, "Tavaran vastaanottopaikka ja -päivä (paikka, maa, päivä)", "Place and date of taking over the goods"),
          f"{place(frm)}\n{d(ctx['dates'].get('taking_over'), lang)}", num="4")
    y += 13
    s.box(X, y, half, 12, t(lang, "Liitetyt asiakirjat", "Documents attached"),
          ", ".join(x for x in [t(lang, "Kauppalasku", "Commercial invoice"), t(lang, "Pakkausluettelo", "Packing list")] +
                    ([t(lang, "VAK-ilmoitus", "DG declaration")] if ctx["dangerous_goods"] else [])), num="5")
    s.box(X + half, y, half, 12, t(lang, "Lähettäjän ohjeet (tulli ym.)", "Sender's instructions (customs etc.)"),
          (t(lang, "Tullaus: ", "Customs: ") + (ctx["customs"].get("lrn") or ctx["customs"].get("mrn") or t(lang, "tullataan määräpaikassa", "cleared at destination"))) if ctx["customs"].get("required") else "", num="13")
    y += 12
    hdr = [t(lang, "6 Merkit ja numerot", "6 Marks and numbers"), t(lang, "7 Kolliluku", "7 Number of packages"),
           t(lang, "8 Pakkaustapa", "8 Method of packing"), t(lang, "9 Tavaran laji", "9 Nature of the goods"),
           t(lang, "10 Tilastonumero", "10 Statistical number"), t(lang, "11 Bruttopaino kg", "11 Gross weight kg"),
           t(lang, "12 Tilavuus m³", "12 Volume m³")]
    rows = []
    for g in ctx["goods"]:
        rows.append([g.get("marks") or (ctx["shipment"]["reference"] if g["seq"] == 1 else ""), n(g.get("packages"), lang),
                     g.get("package_type") or "", g.get("description") or "", g.get("hs_code") or "", n(g.get("gross_kg"), lang, 1),
                     n(g.get("volume_m3"), lang, 2)])
    tot = ctx["totals"]
    rows.append(["", n(tot.get("packages"), lang), "", t(lang, "Yhteensä", "Total") + (f" · LDM {n(tot['ldm'], lang, 1)}" if tot.get("ldm") else ""),
                 "", n(tot.get("gross_kg"), lang, 1), n(tot.get("volume_m3"), lang, 2)])
    widths = [27, 18, 20, 62, 20, 20, Wd - 167]
    rest = s.table(X, y, Wd, 56, hdr, rows, widths, align_right={1, 5, 6})
    y += 56
    if ctx["dangerous_goods"]:
        g = ctx["dangerous_goods"][0]
        s.box(X, y, Wd, 8, t(lang, "Luokka / numero / kirjain / ADR", "Class / number / letter / ADR"),
              f"{g['un_number']} {g.get('proper_shipping_name') or ''}, {g['class']}" + (f", PG {g['packing_group']}" if g.get("packing_group") else "") + ", (E)")
        y += 8
    s.box(X, y, half * 0.6, 14, t(lang, "Maksumääräykset", "Instructions as to payment for carriage"), freight_terms(ctx, lang), num="14")
    s.box(X + half * 0.6, y, half * 0.4, 14, t(lang, "Jälkivaatimus", "Cash on delivery"), "", num="15")
    s.box(X + half, y, half, 14, t(lang, "Erityiset sopimukset", "Special agreements"),
          t(lang, "Huolitsija sopimusrahdinkuljettajana, NSAB 2015.", "Forwarder as contracting carrier, NSAB 2015."), num="19")
    y += 14
    s.box(X, y, Wd, 9, t(lang, "Laadittu (paikka, päivä)", "Established in (place, date)"),
          f"{ctx['issuer']['address'].get('city') or 'Helsinki'}, {d(ctx['dates']['issue'], lang)}", num="21")
    y += 9
    third = Wd / 3
    s.sign(X, y, third, 16, t(lang, "22 Lähettäjän allekirjoitus ja leima", "22 Signature and stamp of the sender"),
           f"{ctx['issuer']['name']} {t(lang, 'lähettäjän puolesta', 'on behalf of the sender')}")
    s.box(X + third, y, third, 16, t(lang, "23 Rahdinkuljettajan allekirjoitus ja leima", "23 Signature and stamp of the carrier"),
          leg.get("carrier") or "")
    s.box(X + 2 * third, y, third, 16, t(lang, "24 Tavara vastaanotettu (paikka, päivä, allekirjoitus)", "24 Goods received (place, date, signature)"), "")
    y += 17
    s.clause(X, y, Wd, 10, t(lang, "Tähän kuljetukseen sovelletaan, sopimuksen vastaisista ehdoista huolimatta, CMR-yleissopimusta.",
                             "This carriage is subject, notwithstanding any clause to the contrary, to the Convention on the Contract for "
                             "the International Carriage of Goods by Road (CMR)."))
    s.footer()
    if rest:
        s.rows_overflow(title, hdr, rest, widths, {1, 5, 6})


def _commercial_invoice(s: Sheet):
    ctx, lang = s.ctx, s.lang
    title = t(lang, "KAUPPALASKU", "COMMERCIAL INVOICE")
    y = s.header(title, t(lang, "Laadittu viejän puolesta lähetyksen tiedoista", "Prepared on behalf of the exporter from the shipment data"))
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 26, t(lang, "Myyjä / viejä", "Seller / exporter"), party(ctx["shipper"], lang))
    s.box(X + half, y, half / 2, 9, t(lang, "Laskun numero", "Invoice number"), s.meta["doc_no"], bold=True)
    s.box(X + half * 1.5, y, half / 2, 9, t(lang, "Päivä", "Date"), d(ctx["dates"]["issue"], lang))
    s.box(X + half, y + 9, half / 2, 9, t(lang, "Asiakkaan viite / tilaus", "Customer ref / PO"),
          " · ".join(x for x in [ctx["refs"].get("customer") or "", ", ".join(ctx["refs"].get("po") or [])] if x))
    s.box(X + half * 1.5, y + 9, half / 2, 9, t(lang, "Toimitusehto", "Incoterms® 2020"),
          f"{ctx['shipment'].get('incoterm') or ''} {ctx['shipment'].get('incoterm_place') or ''}".strip())
    s.box(X + half, y + 18, half / 2, 8, t(lang, "Valuutta", "Currency"), ctx["totals"].get("currency") or "EUR")
    s.box(X + half * 1.5, y + 18, half / 2, 8, t(lang, "Kuljetusmuoto", "Mode of transport"),
          MODE.get(ctx["shipment"]["mode"], ("", ""))[0 if lang == "fi" else 1])
    y += 26
    s.box(X, y, half, 25, t(lang, "Ostaja / vastaanottaja", "Buyer / consignee"), party(ctx["consignee"], lang))
    s.box(X + half, y, half, 25, t(lang, "Lähetys", "Shipment"), "\n".join([
        f"{t(lang, 'Lähtö', 'From')}: {place(ctx['origin'])}", f"{t(lang, 'Määränpää', 'To')}: {place(ctx['destination'])}",
        f"{t(lang, 'Viite', 'Reference')}: {ctx['shipment']['reference']}",
        f"{t(lang, 'Alkuperämaa', 'Country of origin')}: {', '.join(dict.fromkeys(g['origin_country'] for g in ctx['goods'] if g.get('origin_country'))) or '—'}"]))
    y += 25
    hdr = ["#", t(lang, "Kuvaus", "Description"), "HS / CN", t(lang, "Alkuperä", "Origin"), t(lang, "Määrä", "Qty"),
           t(lang, "Yks.", "Unit"), t(lang, "À-hinta", "Unit price"), t(lang, "Summa", "Amount")]
    rows = [[g["seq"], g.get("description") or "", g.get("hs_code") or "", g.get("origin_country") or "", n(g.get("quantity"), lang, 0),
             g.get("unit") or "", n(g.get("unit_price"), lang, 2), n(g.get("amount"), lang, 2)] for g in ctx["goods"]]
    widths = [7, 70, 21, 14, 15, 13, 20, Wd - 160]
    rest = s.table(X, y, Wd, 100, hdr, rows, widths, align_right={4, 6, 7})
    y += 100
    tot = ctx["totals"]
    s.box(X, y, Wd * 0.25, 10, t(lang, "Kolleja", "Packages"), f"{n(tot.get('packages'), lang)} {tot.get('package_type') or ''}")
    s.box(X + Wd * 0.25, y, Wd * 0.25, 10, t(lang, "Nettopaino", "Net weight"), kg(tot.get("net_kg"), lang))
    s.box(X + Wd * 0.5, y, Wd * 0.25, 10, t(lang, "Bruttopaino", "Gross weight"), kg(tot.get("gross_kg"), lang))
    s.box(X + Wd * 0.75, y, Wd * 0.25, 10, t(lang, "Yhteensä", "Total"), money(tot.get("amount"), tot.get("currency"), lang), bold=True, fill=TINT)
    y += 12
    s.clause(X, y, Wd, 14, t(lang, "Vakuutamme, että laskun tiedot ovat oikeat ja että tavaroiden alkuperä on ilmoitettu oikein. "
                                   "Huolitsija on laatinut laskun viejän toimittamista tiedoista; viejä vastaa sisällöstä.",
                             "We certify that this invoice is true and correct and that the origin of the goods is as stated. "
                             "Prepared by the forwarder from data supplied by the exporter, who is responsible for its content."))
    y += 14
    s.sign(X + half, y, half, 14, t(lang, "Viejän puolesta", "For the exporter"), (ctx["shipper"] or {}).get("name") or "")
    s.footer()
    if rest:
        s.rows_overflow(title, hdr, rest, widths, {4, 6, 7})


def _packing_list(s: Sheet):
    ctx, lang = s.ctx, s.lang
    title = t(lang, "PAKKAUSLUETTELO", "PACKING LIST")
    y = s.header(title, t(lang, "Lähetyksen ja kauppalaskun tiedoista", "From the shipment and the commercial invoice"))
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 25, t(lang, "Lähettäjä", "Shipper"), party(ctx["shipper"], lang))
    s.box(X + half, y, half, 25, t(lang, "Vastaanottaja", "Consignee"), party(ctx["consignee"], lang))
    y += 25
    q = Wd / 4
    s.box(X, y, q, 9, t(lang, "Lähetysviite", "Shipment reference"), ctx["shipment"]["reference"])
    s.box(X + q, y, q, 9, t(lang, "Tilaus", "PO"), ", ".join(ctx["refs"].get("po") or []))
    s.box(X + 2 * q, y, q, 9, t(lang, "Lähtö", "From"), place(ctx["origin"]))
    s.box(X + 3 * q, y, q, 9, t(lang, "Määränpää", "To"), place(ctx["destination"]))
    y += 9
    hdr = ["#", t(lang, "Merkit", "Marks"), t(lang, "Kollit", "Packages"), t(lang, "Sisältö", "Contents"), t(lang, "Määrä", "Qty"),
           t(lang, "Netto kg", "Net kg"), t(lang, "Brutto kg", "Gross kg"), "m³"]
    rows = [[g["seq"], g.get("marks") or ctx["shipment"]["reference"], pkg_text(g, lang), g.get("description") or "",
             f"{n(g.get('quantity'), lang)} {g.get('unit') or ''}".strip(), n(g.get("net_kg"), lang, 1), n(g.get("gross_kg"), lang, 1),
             n(g.get("volume_m3"), lang, 2)] for g in ctx["goods"]]
    tot = ctx["totals"]
    rows.append(["", "", f"{n(tot.get('packages'), lang)} {tot.get('package_type') or ''}", t(lang, "Yhteensä", "Total"), "",
                 n(tot.get("net_kg"), lang, 1), n(tot.get("gross_kg"), lang, 1), n(tot.get("volume_m3"), lang, 2)])
    widths = [7, 28, 24, 66, 18, 17, 17, Wd - 177]
    rest = s.table(X, y, Wd, 120, hdr, rows, widths, align_right={4, 5, 6, 7})
    y += 120
    s.box(X, y, Wd, 16, t(lang, "Kontit ja sinetit", "Containers and seals"), containers_text(ctx, lang) or "—")
    y += 16
    s.sign(X + half, y + 2, half, 14, t(lang, "Viejän puolesta", "For the exporter"), (ctx["shipper"] or {}).get("name") or "")
    s.footer()
    if rest:
        s.rows_overflow(title, hdr, rest, widths, {4, 5, 6, 7})


def _booking_confirmation(s: Sheet):
    ctx, lang = s.ctx, s.lang
    y = s.header(t(lang, "VARAUSVAHVISTUS", "BOOKING CONFIRMATION"), ctx["shipment"]["reference"])
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 24, t(lang, "Asiakas", "Customer"), party(ctx["customer"], lang))
    sh = ctx["shipment"]
    s.box(X + half, y, half, 24, t(lang, "Varaus", "Booking"), "\n".join(x for x in [
        f"{t(lang, 'Lähetys', 'Shipment')}: {sh['reference']}",
        f"{t(lang, 'Kuljetusmuoto', 'Mode')}: {MODE.get(sh['mode'], ('', ''))[0 if lang == 'fi' else 1]}" + (f" · {sh['service']}" if sh.get("service") else ""),
        f"{t(lang, 'Toimitusehto', 'Incoterms® 2020')}: {sh.get('incoterm') or ''} {sh.get('incoterm_place') or ''}",
        f"{t(lang, 'Asiakkaan viite', 'Your reference')}: {sh['customer_ref']}" if sh.get("customer_ref") else "",
        f"{t(lang, 'Varausnumero', 'Carrier booking')}: {sh['booking_no']}" if sh.get("booking_no") else ""] if x))
    y += 24
    s.box(X, y, half, 22, t(lang, "Lähettäjä", "Shipper"), party(ctx["shipper"], lang, ids=False))
    s.box(X + half, y, half, 22, t(lang, "Vastaanottaja", "Consignee"), party(ctx["consignee"], lang, ids=False))
    y += 22
    hdr = ["#", t(lang, "Muoto", "Mode"), t(lang, "Mistä", "From"), t(lang, "Mihin", "To"), t(lang, "Kuljettaja", "Carrier"),
           t(lang, "Lähtö", "ETD"), t(lang, "Saapuminen", "ETA")]
    rows = [[lg["seq"], MODE.get(lg["mode"], ("", ""))[0 if lang == "fi" else 1], place(lg["from"]), place(lg["to"]),
             " ".join(x for x in [lg.get("carrier") or "", lg.get("vessel") or "", flight_label(lg.get("flight"))] if x), d(lg.get("etd"), lang),
             d(lg.get("eta"), lang)] for lg in ctx["legs"]]
    s.table(X, y, Wd, 52, hdr, rows, [7, 17, 43, 43, 44, 18, Wd - 172], label=t(lang, "Reitti", "Route"))
    y += 52
    tot = ctx["totals"]
    s.box(X, y, Wd, 16, t(lang, "Tavara", "Cargo"), "\n".join(x for x in [
        ctx.get("cargo_description") or "", f"{n(tot.get('packages'), lang)} {tot.get('package_type') or ''} · {kg(tot.get('gross_kg'), lang)}"
        + (f" · {n(tot.get('volume_m3'), lang, 1)} m³" if tot.get("volume_m3") else ""), containers_text(ctx, lang).replace("\n", ", ")] if x))
    y += 16
    f = ctx["freight"]
    s.box(X, y, half, 12, t(lang, "Hinta (alv 0 %)", "Price (excl. VAT)"),
          money(f["total_cents"] / 100, f["currency"], lang) if f.get("total_cents") else t(lang, "sopimuksen mukaan", "as agreed"), bold=True)
    s.box(X + half, y, half, 12, t(lang, "Vastuu", "Liability"), sh.get("liability_regime") or "NSAB 2015")
    y += 12
    needs = [t(lang, "Kauppalasku ja pakkausluettelo ennen lähtöä", "Commercial invoice and packing list before departure")]
    if ctx["dangerous_goods"]:
        needs.append(t(lang, "Allekirjoitettu vaarallisten aineiden ilmoitus 48 h ennen lastausta", "Signed dangerous goods declaration 48 h before loading"))
    if ctx["customs"].get("required"):
        needs.append(t(lang, "Tullausvaltakirja (jos ei jo annettu)", "Customs power of attorney (if not given yet)"))
    s.box(X, y, Wd, 18, t(lang, "Tarvitsemme sinulta", "What we need from you"), "\n".join("• " + x for x in needs))
    y += 20
    s.clause(X, y, Wd, 20, t(lang,
        "Toimimme sopimusrahdinkuljettajana (huolitsija päämiehenä). Palvelut suoritetaan Pohjoismaisen Speditööriliiton yleisten "
        "määräysten NSAB 2015 (PSYM 2015) mukaisesti. Tullit, verot ja muut viranomaismaksut eivät sisälly hintaan, ellei niitä ole eritelty.",
        "We act as contracting forwarder (principal). Services are performed under the General Conditions of the Nordic Association of "
        "Freight Forwarders NSAB 2015. Duties, taxes and other authority charges are not included unless itemised."))
    s.footer()


def _arrival_notice(s: Sheet):
    ctx, lang = s.ctx, s.lang
    y = s.header(t(lang, "SAAPUMISILMOITUS", "ARRIVAL NOTICE"), ctx["shipment"]["reference"])
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 24, t(lang, "Vastaanottaja", "Consignee"), party(ctx["consignee"], lang))
    s.box(X + half, y, half, 24, t(lang, "Ilmoitettava osapuoli", "Notify party"), party(ctx["notify"], lang) or t(lang, "Sama kuin vastaanottaja", "Same as consignee"))
    y += 24
    m = ctx["main_leg"]
    mode = ctx["shipment"]["mode"]
    q = Wd / 4
    tdoc = {"sea": t(lang, "House B/L", "House B/L"), "air": "HAWB", "road": "CMR"}.get(mode, t(lang, "Rahtikirja", "Transport document"))
    s.box(X, y, q, 10, tdoc, ctx.get("transport_doc_no") or ctx["refs"].get("mbl") or ctx["refs"].get("awb_master") or "")
    s.box(X + q, y, q, 10, t(lang, "Alus / lento", "Vessel / flight"), vessel_voyage(ctx) if mode == "sea" else (flight_label(m.get("flight")) or m.get("carrier") or ""))
    s.box(X + 2 * q, y, q, 10, t(lang, "Purkaussatama / -asema", "Port of discharge"), place(ctx["port_of_discharge"]))
    s.box(X + 3 * q, y, q, 10, t(lang, "Saapunut / arvio", "Arrived / ETA"), d(m.get("ata") or ctx["shipment"].get("ata"), lang) or d(m.get("eta") or ctx["shipment"]["eta"], lang), bold=True)
    y += 10
    s.box(X, y, q, 10, t(lang, "Lähtösatama", "Port of loading"), place(ctx["port_of_loading"]))
    s.box(X + q, y, q, 10, t(lang, "Määräpaikka", "Place of delivery"), place(ctx["place_of_delivery"]))
    s.box(X + 2 * q, y, q, 10, t(lang, "Lähettäjä", "Shipper"), (ctx["shipper"] or {}).get("name") or "")
    s.box(X + 3 * q, y, q, 10, t(lang, "Asiakkaan viite", "Your reference"), ctx["refs"].get("customer") or "")
    y += 10
    hdr = [t(lang, "Kontti / merkit", "Container / marks"), t(lang, "Kollit", "Packages"), t(lang, "Kuvaus", "Description"),
           t(lang, "Bruttopaino", "Gross weight")]
    rows = [[containers_text(ctx, lang) if g["seq"] == 1 else "", pkg_text(g, lang), g.get("description") or "", kg(g.get("gross_kg"), lang)]
            for g in ctx["goods"]]
    s.table(X, y, Wd, 60, hdr, rows, [48, 28, 86, Wd - 162], align_right={3})
    y += 60
    cu = ctx["customs"]
    if cu.get("required"):
        status = {"released": t(lang, "luovutettu", "released"), "accepted": t(lang, "hyväksytty", "accepted")}.get(cu.get("status") or "", cu.get("status") or t(lang, "ei vielä ilmoitettu", "not yet filed"))
        ctext = f"{t(lang, 'Tullausilmoitus', 'Customs declaration')} {cu.get('lrn') or ''} · {status}" + (f" · MRN {cu['mrn']}" if cu.get("mrn") else "")
    else:
        ctext = t(lang, "Ei tullausta (EU:n sisäinen tai asiakkaan oma tullaus)", "No customs clearance by us (intra-EU or customer's own)")
    s.box(X, y, Wd, 10, t(lang, "Tulli", "Customs"), ctext)
    y += 10
    f = ctx["freight"]
    # The shipment price is not an invoice balance or evidence of payment.
    # In particular, missing values and prepaid terms must never imply paid/zero due.
    amount = (money(f["total_cents"] / 100, f.get("currency"), lang)
              if f.get("total_cents") is not None and f.get("currency")
              else t(lang, "Rahtimäärä puuttuu", "Freight amount unavailable"))
    terms = {"collect": "Collect", "prepaid": "Prepaid"}.get(f.get("terms"),
             t(lang, "Maksuehto puuttuu", "Freight terms unavailable"))
    due = f"{terms}: {amount}\n" + t(lang, "Maksutilaa ei vahvistettu", "Payment status unverified")
    s.box(X, y, half, 12, t(lang, "Rahti / maksutila", "Freight / payment status"), due, bold=True)
    s.box(X + half, y, half, 12, t(lang, "Kotiinkuljetus", "On-carriage"), (ctx.get("delivery_carrier") or {}).get("name") or t(lang, "Asiakas noutaa", "Collected by the consignee"))
    y += 14
    s.clause(X, y, Wd, 24, t(lang,
        "Tavara on saapunut tai saapuu yllä mainittuna päivänä. Luovutamme tavaran, kun tulli on sen vapauttanut ja mahdolliset maksut on "
        "suoritettu. Satamaterminaalin vapaa-aika on rajallinen; varastointi- ja konttiseisontamaksut veloitetaan voimassa olevan hinnaston mukaan.",
        "The goods have arrived or will arrive on the date shown. We release them once customs has cleared them and any amounts due are paid. "
        "Terminal free time is limited; storage, demurrage and detention are charged at the current tariff."))
    s.footer()


def _delivery_order(s: Sheet):
    ctx, lang = s.ctx, s.lang
    y = s.header(t(lang, "LUOVUTUSMÄÄRÄYS", "DELIVERY ORDER"), ctx["shipment"]["reference"])
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    last = ctx["legs"][-1] if ctx["legs"] else {}
    release = ctx.get("delivery_release") or {}
    unavailable = t(lang, "Luovutusvaltuutus puuttuu", "Release authorization missing")
    s.box(X, y, half, 20, t(lang, "Kenelle (terminaali / varasto)", "To (terminal / warehouse)"), release.get("terminal") or unavailable)
    s.box(X + half, y, half, 20, t(lang, "Luovuta kuljettajalle", "Release to haulier"),
          "\n".join(x for x in [release.get("haulier") or unavailable,
                                (t(lang, "Rekisterinumero", "Truck") + f": {last.get('truck_plate')}") if last.get("truck_plate") else ""] if x))
    y += 20
    s.box(X, y, half, 24, t(lang, "Vastaanottaja", "Consignee"), party(ctx["consignee"], lang))
    s.box(X + half, y, half, 24, t(lang, "Toimitusosoite", "Delivery address"),
          "\n".join(x for x in [place(ctx["place_of_delivery"]), " ".join(v for v in ((ctx["place_of_delivery"].get("address") or {}).get(k) for k in ("street", "postal_code", "city")) if v)] if x))
    y += 24
    cu = ctx["customs"]
    q = Wd / 4
    s.box(X, y, q, 10, t(lang, "Tullin luovutus", "Customs release"),
          ((f"MRN {cu['mrn']}" if cu.get("mrn") else t(lang, "Luovutustieto puuttuu", "Release data missing")) + (f"\n{d(cu.get('released_at'), lang)}" if cu.get("released_at") else ""))
          if cu.get("required") else t(lang, "Ei tullausta huolitsijalta", "No clearance by forwarder"))
    s.box(X + q, y, q, 10, t(lang, "Alus / lento / liikennemuoto", "Vessel / flight / mode"),
          vessel_voyage(ctx) or flight_label(ctx["main_leg"].get("flight")) or MODE.get(ctx["shipment"]["mode"], ("Tieto puuttuu", "Unavailable"))[0 if lang == "fi" else 1])
    s.box(X + 2 * q, y, q, 10, t(lang, "Rahtikirja", "Transport document"), release.get("transport_document") or unavailable)
    s.box(X + 3 * q, y, q, 10, t(lang, "Voimassa", "Valid until"), release.get("valid_until") or unavailable, size=7)
    y += 10
    hdr = [t(lang, "Kontti / merkit", "Container / marks"), t(lang, "Kollit", "Packages"), t(lang, "Kuvaus", "Description"), t(lang, "Bruttopaino", "Gross weight")]
    rows = [[containers_text(ctx, lang) if g["seq"] == 1 else "", pkg_text(g, lang), g.get("description") or "", kg(g.get("gross_kg"), lang)] for g in ctx["goods"]]
    s.table(X, y, Wd, 70, hdr, rows, [48, 28, 86, Wd - 162], align_right={3})
    y += 70
    instruction = (t(lang, "Luonnos — ei oikeuta tavaran luovutukseen.", "Draft — does not authorize cargo release.")
                   if s.meta.get("status") == "draft" else
                   t(lang, "Luovutus yllä nimetylle kuljettajalle lähdevaltuutuksen ehtojen mukaisesti.", "Release to the named haulier subject to the source authorization's conditions."))
    provenance = (f"{release['issuer']} · {release['reference']} · " + t(lang, "Käsin tarkastettu", "Manually reviewed")) if release else unavailable
    s.box(X, y, Wd, 14, t(lang, "Ohjeet / luovutusvaltuutus", "Instructions / release authority"), instruction + "\n" + provenance, size=7)
    y += 14
    s.sign(X + half, y + 2, half, 14, t(lang, "Huolitsijan puolesta", "For the forwarder"), ctx["issuer"]["name"], d(ctx["dates"]["issue"], lang))
    s.footer()


def _plus_days(iso: str | None, days: int) -> str | None:
    from datetime import timedelta
    try:
        return (date.fromisoformat(str(iso)[:10]) + timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return None


def _certificate_of_origin(s: Sheet):
    ctx, lang = s.ctx, s.lang
    y = s.header(t(lang, "ALKUPERÄTODISTUS — HAKEMUS", "CERTIFICATE OF ORIGIN — APPLICATION"),
                 t(lang, "Kauppakamarin myöntämä, ei-preferentiaalinen alkuperä (UCC 59–63)", "Issued by the Chamber of Commerce, non-preferential origin (UCC art. 59-63)"))
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 26, t(lang, "Viejä", "Consignor / exporter"), party(ctx["shipper"], lang), num="1")
    s.box(X + half, y, half, 26, t(lang, "Myöntäjä", "Issuing body"), t(lang, "Kauppakamari (Suomen Kauppakamarit)\nAlkuperätodistus myönnetään hakemuksen ja liitteiden perusteella.",
          "Chamber of Commerce (Finland Chamber of Commerce)\nThe certificate is issued on the basis of this application and its annexes."))
    y += 26
    s.box(X, y, half, 24, t(lang, "Vastaanottaja", "Consignee"), party(ctx["consignee"], lang), num="2")
    origins = ", ".join(dict.fromkeys(g["origin_country"] for g in ctx["goods"] if g.get("origin_country"))) or ""
    s.box(X + half, y, half, 24, t(lang, "Alkuperämaa", "Country of origin"), origins, num="3", bold=True, size=11)
    y += 24
    s.box(X, y, half, 16, t(lang, "Kuljetustiedot", "Transport details"),
          f"{MODE.get(ctx['shipment']['mode'], ('', ''))[0 if lang == 'fi' else 1]} · {place(ctx['port_of_loading'])} – {place(ctx['port_of_discharge'])}\n{vessel_voyage(ctx)}", num="4")
    s.box(X + half, y, half, 16, t(lang, "Huomautuksia", "Remarks"), f"{t(lang, 'Kauppalasku', 'Invoice')}: " +
          ", ".join(ctx["refs"].get("po") or []) + f" · {ctx['shipment']['reference']}", num="5")
    y += 16
    hdr = [t(lang, "6 Nro, merkit, kollit ja tavaran kuvaus", "6 Item no., marks, packages, description of goods"), t(lang, "7 Määrä", "7 Quantity")]
    rows = [[f"{g['seq']}. {ctx['shipment']['reference']} — {pkg_text(g, lang)} {g.get('description') or ''}" + (f" (HS {g['hs_code']})" if g.get("hs_code") else ""),
             f"{n(g.get('quantity'), lang)} {g.get('unit') or ''} · {kg(g.get('gross_kg'), lang)}"] for g in ctx["goods"]]
    s.table(X, y, Wd, 80, hdr, rows, [Wd - 50, 50])
    y += 80
    s.box(X, y, Wd, 20, t(lang, "Hakijan vakuutus", "Declaration by the applicant"), t(lang,
          "Allekirjoittanut vakuuttaa, että yllä kuvatut tavarat ovat alkuperältään ilmoitetusta maasta ja että tiedot ovat oikeat. "
          "Hakija sitoutuu esittämään alkuperää osoittavat asiakirjat kauppakamarin pyynnöstä.",
          "The undersigned declares that the goods described above originate in the country shown in box 3 and that the particulars are "
          "correct, and undertakes to submit the supporting evidence of origin at the Chamber's request."), size=7.4)
    y += 20
    s.sign(X + half, y, half, 14, t(lang, "Viejän puolesta (huolitsija valtakirjalla)", "For the exporter (forwarder under power of attorney)"),
           ctx["issuer"]["name"], d(ctx["dates"]["issue"], lang))
    s.footer()


def _eur1(s: Sheet):
    ctx, lang = s.ctx, s.lang
    y = s.header("EUR.1", t(lang, "Tavaratodistus — hakemus (Tulli)", "Movement certificate — application (Finnish Customs)"))
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 24, t(lang, "Viejä (nimi, osoite, maa)", "Exporter (name, full address, country)"), party(ctx["shipper"], lang), num="1")
    s.box(X + half, y, half, 24, "EUR.1", t(lang, "Nro", "No.") + f" {s.meta['doc_no']}\n" + t(lang,
          "Katso huomautukset kääntöpuolella ennen lomakkeen täyttämistä.", "See notes overleaf before completing this form."), num="")
    y += 24
    s.box(X, y, half, 22, t(lang, "Vastaanottaja (vapaaehtoinen)", "Consignee (optional)"), party(ctx["consignee"], lang), num="3")
    s.box(X + half, y, half, 22, t(lang, "Todistus etuuskohtelukaupassa välillä", "Certificate used in preferential trade between"),
          f"{t(lang, 'Euroopan unioni', 'the European Union')}\n{t(lang, 'ja', 'and')}\n{ctx['destination']['country']}", num="2")
    y += 22
    s.box(X, y, half / 2, 12, t(lang, "Alkuperämaa / -alue", "Country, group of countries or territory of origin"),
          ", ".join(sorted({g["origin_country"] for g in ctx["goods"] if g.get("origin_country")}))
          or t(lang, "Alkuperätieto puuttuu", "Origin data missing"), num="4", bold=True)
    s.box(X + half / 2, y, half / 2, 12, t(lang, "Määrämaa", "Country of destination"), ctx["destination"]["country"], num="5", bold=True)
    s.box(X + half, y, half, 24, t(lang, "Huomautuksia", "Remarks"), "", num="7")
    y += 12
    s.box(X, y, half, 12, t(lang, "Kuljetustiedot (vapaaehtoinen)", "Transport details (optional)"),
          f"{MODE.get(ctx['shipment']['mode'], ('', ''))[0 if lang == 'fi' else 1]} {place(ctx['port_of_loading'])} – {place(ctx['port_of_discharge'])}", num="6")
    y += 12
    hdr = [t(lang, "8 Nro; merkit; kollien määrä ja laji; tavaran kuvaus", "8 Item no.; marks and numbers; number and kind of packages; description of goods"),
           t(lang, "9 Bruttopaino tai muu määrä", "9 Gross mass (kg) or other measure"), t(lang, "10 Laskut", "10 Invoices")]
    rows = [[f"{g['seq']}. {pkg_text(g, lang)} {g.get('description') or ''}" + (f" — HS {g['hs_code']}" if g.get("hs_code") else ""),
             kg(g.get("gross_kg"), lang), ctx["refs"].get("invoice") or t(lang, "Ei ilmoitettu", "Not supplied")] for g in ctx["goods"]]
    s.table(X, y, Wd, 66, hdr, rows, [Wd - 80, 40, 40])
    y += 66
    s.box(X, y, half, 32, t(lang, "Tullin vahvistus", "Customs endorsement"), t(lang, "Tulli täyttää", "To be completed by Customs"), num="11")
    s.box(X + half, y, half, 32, t(lang, "Viejän vakuutus", "Declaration by the exporter"), t(lang,
          "Allekirjoittanut viejä vakuuttaa, että edellä mainitut tavarat täyttävät tämän todistuksen antamiseen vaadittavat edellytykset.",
          "I, the undersigned, declare that the goods described above meet the conditions required for the issue of this certificate.")
          + f"\n{(ctx['shipper'] or {}).get('name') or ''}, {d(ctx['dates']['issue'], lang)}", num="12", size=7.2)
    y += 34
    s.clause(X, y, Wd, 20, t(lang,
        "Hakemus, ei myönnetty tavaratodistus. Kohdan 4 alkuperämaat perustuvat lähtötietoihin. Viejän on ennen allekirjoitusta "
        "varmistettava etuuskohtelusopimuksen alkuperäsäännöt ja niitä tukevat todisteet. Lähtömaa ei osoita tavaran alkuperää.",
        "Application, not an issued movement certificate. Box 4 records the supplied goods origins. Before signing, the exporter "
        "must confirm the agreement's preferential origin rules and supporting evidence. The departure country does not prove goods origin."))
    s.footer()


def _dg(s: Sheet):
    ctx, lang = s.ctx, s.lang
    air = ctx["shipment"]["mode"] == "air"
    if air:
        title = t(lang, "LÄHETTÄJÄN ILMOITUS VAARALLISISTA AINEISTA", "SHIPPER'S DECLARATION FOR DANGEROUS GOODS")
        sub = "IATA DGR 8.1"
    else:
        title = t(lang, "VAARALLISTEN AINEIDEN ILMOITUS (MULTIMODAALI)", "MULTIMODAL DANGEROUS GOODS FORM")
        sub = "IMDG Code 5.4.5 · ADR 5.4.1"
    y = s.header(title, sub)
    X, Wd = M / mm, (W - 2 * M) / mm
    half = Wd / 2
    s.box(X, y, half, 24, t(lang, "Lähettäjä", "Shipper / consignor / sender"), party(ctx["shipper"], lang), num="1")
    s.box(X + half, y, half / 2, 12, t(lang, "Kuljetusasiakirjan nro", "Transport document number"), ctx["shipment"]["reference"], num="2")
    s.box(X + half * 1.5, y, half / 2, 12, t(lang, "Sivu", "Page"), "1 / 1", num="3")
    s.box(X + half, y + 12, half, 12, t(lang, "Lähettäjän viite", "Shipper's reference"), ctx["refs"].get("customer") or "", num="4")
    y += 24
    s.box(X, y, half, 22, t(lang, "Vastaanottaja", "Consignee"), party(ctx["consignee"], lang), num="6")
    s.box(X + half, y, half, 22, t(lang, "Rahdinkuljettaja", "Carrier (to be completed by the carrier)"), ctx["main_leg"].get("carrier") or "", num="7")
    y += 22
    if air:
        s.box(X, y, half, 10, t(lang, "Lähtöasema", "Airport of departure"), place(ctx["port_of_loading"]))
        s.box(X + half, y, half, 10, t(lang, "Määräasema", "Airport of destination"), place(ctx["port_of_discharge"]))
        y += 10
        s.box(X, y, Wd, 10, t(lang, "Kuljetus", "Shipment type"), t(lang,
              "Rajoitukset: matkustaja- ja rahtikone / vain rahtikone — katso pakkausohje", "This shipment is within the limitations prescribed for: PASSENGER AND CARGO AIRCRAFT / CARGO AIRCRAFT ONLY (delete non-applicable)"))
        y += 10
    else:
        s.box(X, y, half / 2, 10, t(lang, "Alus ja matka", "Vessel / flight no. and date"), vessel_voyage(ctx), num="10")
        s.box(X + half / 2, y, half / 2, 10, t(lang, "Lastaussatama", "Port / place of loading"), place(ctx["port_of_loading"]), num="11")
        s.box(X + half, y, half / 2, 10, t(lang, "Purkaussatama", "Port / place of discharge"), place(ctx["port_of_discharge"]), num="12")
        s.box(X + half * 1.5, y, half / 2, 10, t(lang, "Määräpaikka", "Destination"), place(ctx["place_of_delivery"]), num="13")
        y += 10
    hdr = [t(lang, "UN-nro", "UN No."), t(lang, "Virallinen kuljetusnimi", "Proper shipping name"), t(lang, "Luokka (lisävaara)", "Class (subsidiary)"),
           "PG", t(lang, "Kollit ja laji", "Packages, kind"), t(lang, "Brutto / netto", "Gross / net"), "EmS / " + t(lang, "lisätiedot", "notes")]
    rows = [[g["un_number"], g.get("proper_shipping_name") or "", f"{g['class']}" + (f" ({g['subsidiary']})" if g.get("subsidiary") else ""),
             g.get("packing_group") or "—", f"{n(g.get('packages'), lang)} {g.get('package_type') or ''}",
             " / ".join(x for x in [kg(g.get("gross_kg"), lang), kg(g.get("net_kg"), lang)] if x),
             ", ".join(x for x in [g.get("ems") or "", t(lang, "Meriä saastuttava", "Marine pollutant") if g.get("marine_pollutant") else "",
                                   t(lang, "Rajoitettu määrä", "Limited quantity") if g.get("limited_quantity") else "",
                                   f"SP {g['special_provisions']}" if g.get("special_provisions") else ""] if x)] for g in ctx["dangerous_goods"]]
    s.table(X, y, Wd, 60, hdr, rows, [16, 58, 22, 10, 26, 28, Wd - 160], label=t(lang, "Merkit, kollit, kuvaus", "Marks & numbers, packages, description of goods"), num="14")
    y += 60
    if not air and ctx["containers"]:
        c0 = ctx["containers"][0]
        s.box(X, y, half, 14, t(lang, "Kontin tunnus / sinetti", "Container identification / seal"), f"{c0['number']} · {c0.get('type') or ''}", num="15")
        s.box(X + half, y, half, 14, t(lang, "Kontin pakkaustodistus", "Container packing certificate"), t(lang,
              "Vakuutan, että kontti on pakattu ja kuormattu IMDG-säännöstön 5.4.2 mukaisesti.", "I hereby declare that the goods have been packed/loaded into the container in accordance with IMDG Code 5.4.2."), num="16", size=7)
        y += 14
    s.box(X, y, Wd, 26, t(lang, "Lähettäjän vakuutus", "Shipper's declaration"), t(lang,
          "Vakuutan, että lähetyksen sisältö on yllä täydellisesti ja täsmällisesti kuvattu virallisella kuljetusnimellä, luokiteltu, pakattu, "
          "merkitty ja varustettu varoitusmerkinnöin ja on kaikilta osin kuljetuskelpoisessa kunnossa sovellettavien kansainvälisten ja "
          "kansallisten määräysten mukaisesti.",
          "I hereby declare that the contents of this consignment are fully and accurately described above by the proper shipping name, and "
          "are classified, packaged, marked and labelled/placarded and are in all respects in proper condition for transport according to the "
          "applicable international and national governmental regulations."), num="17", size=7.4)
    y += 26
    s.box(X, y, half, 16, t(lang, "Allekirjoittaja (lähettäjä)", "Name / status, company of declarant"),
          (ctx["shipper"] or {}).get("name") or "", num="18")
    s.box(X + half, y, half, 16, t(lang, "Paikka, päivä ja allekirjoitus", "Place and date, signature of declarant"),
          t(lang, "LÄHETTÄJÄN ALLEKIRJOITUS VAADITAAN — huolitsija ei allekirjoita vaarallisten aineiden ilmoitusta lähettäjän puolesta.",
            "SHIPPER'S SIGNATURE REQUIRED — the forwarder does not sign the dangerous goods declaration on the shipper's behalf."), size=7, num="19")
    y += 18
    s.clause(X, y, Wd, 12, t(lang, "Tiedot: YK:n vaarallisten aineiden luettelo (49 CFR 172.101 -taulukon kautta). Lähettäjä vastaa luokituksesta.",
                             "Data: UN Dangerous Goods List (via the 49 CFR 172.101 table). The shipper is responsible for classification."))
    s.footer()


RENDERERS = {"master_bl": _house_bl, "house_bl": _house_bl, "hawb": _hawb, "cmr": _cmr, "commercial_invoice": _commercial_invoice,
             "packing_list": _packing_list, "booking_confirmation": _booking_confirmation, "arrival_notice": _arrival_notice,
             "delivery_order": _delivery_order, "certificate_of_origin": _certificate_of_origin, "eur1_application": _eur1,
             "dg_declaration": _dg}


def render(kind: str, ctx: dict, *, lang: str = "fi", meta: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=1)
    title = catalog.KINDS[kind]["title"]["fi" if lang == "fi" else "en"]
    c.setTitle(f"{title} {meta['doc_no']} v{meta['version']}")
    c.setAuthor(ctx["issuer"].get("name") or config.COMPANY_NAME)
    c.setSubject(ctx["shipment"]["reference"] or "")
    c.setCreator("Huolitsija trade documents")
    RENDERERS[kind](Sheet(c, lang, kind, ctx, meta))
    c.showPage()
    c.save()
    return buf.getvalue()
