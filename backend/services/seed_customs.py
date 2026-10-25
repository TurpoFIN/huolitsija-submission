"""Demo documents and customs declarations (startup order 40, after V1's shipments at 30). SEED_DEMO only,
idempotent (marker seed:customs:v1), every record demo: true.

For some of V1's seeded import shipments from outside the EU it generates a commercial invoice, a packing list
and (sea) a bill of lading as PDFs with reportlab. Each page is stamped "SAMPLE DOCUMENT — demo data". The
extraction results are the values the PDFs were generated from, marked source "seed" (no AI was run on them).
Declarations are drafted with the codes printed on the invoices (no AI call) and moved to varied states.
Duty rates are demo values labelled "seed — verify in TARIC".

The PDF renderers are also used by the V3 tests to produce real-looking invoices for the extraction agent.
"""
from __future__ import annotations

import io
import logging
import random
from datetime import timedelta

from core import config
from core.audit import agent_actor
from core.db import db
from core.lifecycle import on_startup
from core.util import NO_ID, now, now_iso, parse_iso

log = logging.getLogger("seed_customs")
MARKER = "seed:customs:v1"
SAMPLE = "SAMPLE DOCUMENT — demo data for a Huolitsija demo company, not a real transaction"

# Catalogue per demo company (services/seed_core.DEMO_COMPANIES keys). Codes are CN 8-digit codes printed on
# the supplier invoice; duty is a demo value (verify in TARIC). Weights are per unit (kg).
CATALOG = {
    "kaluste": {"goods": ("Oak dining chairs, tables and sideboards (LCL)", "Tammiset ruokatuolit, ruokapöydät ja senkit (LCL)"), "seller": ("Binh Duong Woodcraft Co., Ltd.", "Lot C4, My Phuoc 2 Industrial Park, Ben Cat, Binh Duong", "VN",
                           "3702145578"), "currency": "USD", "incoterm": ("FOB", "Cat Lai, Ho Chi Minh City"),
                "origin_text": True,
                "lines": [("Solid oak dining chair with upholstered fabric seat, model NORA", "94016100", "pcs", 38.5, 7.2, 8.1, 0.0, 4),
                          ("Extendable dining table, solid oak, 180-240 cm, model NORA", "94036010", "pcs", 214.0, 48.0, 55.0, 0.0, 1),
                          ("Oak sideboard with three drawers, model LINEA", "94036010", "pcs", 176.0, 41.0, 47.5, 0.0, 1)]},
    "bike": {"goods": ("Bicycle disc brakes, rims and chains", "Polkupyörien levyjarrut, vanteet ja ketjut"), "seller": ("Taichung Precision Cycle Components Co., Ltd.", "No. 88, Gongye 2nd Rd, Xitun District, Taichung", "TW",
                        "53412876"), "currency": "USD", "incoterm": ("FCA", "Taichung"), "origin_text": False,
             "lines": [("Hydraulic disc brake set for bicycles, front and rear, with 160 mm rotors", "87149420", "sets", 24.9, 0.62, 0.8, 4.7, 1),
                       ("Aluminium alloy bicycle rims 29 inch, 32 hole, tubeless ready", "87149210", "pcs", 17.4, 0.48, 0.6, 4.7, 1),
                       ("Bicycle roller chains 12-speed, nickel plated steel", "73151110", "pcs", 9.8, 0.26, 0.31, 2.7, 1)]},
    "coffee": {"goods": ("Green arabica coffee, Brazil Sul de Minas, 60 kg bags", "Raakakahvi (arabica), Brasilia Sul de Minas, 60 kg:n säkit"), "seller": ("Cooperativa Cafeeira Sul de Minas (sample)", "Rua Coronel Pedro Alves 210, Varginha, MG", "BR",
                          "17.894.556/0001-20"), "currency": "USD", "incoterm": ("FOB", "Santos"), "origin_text": False,
               "lines": [("Green arabica coffee beans, not roasted, not decaffeinated, Brazil Sul de Minas NY2 17/18, 60 kg jute bags",
                          "09011100", "kg", 5.42, 1.0, 1.012, 0.0, 60)]},
    "electro": {"goods": ("Microcontrollers and ceramic capacitors on reels", "Mikro-ohjaimet ja keraamiset kondensaattorit keloilla"), "seller": ("Shenzhen Huaxin Microelectronics Co., Ltd. (sample)", "Building 5, Keji Park, Nanshan District, Shenzhen", "CN",
                           "91440300MA5DEMO01"), "currency": "USD", "incoterm": ("EXW", "Shenzhen"), "origin_text": False,
                "lines": [("32-bit ARM microcontroller integrated circuits, LQFP-64 package, on reels", "85423190", "pcs", 2.35, 0.0006, 0.0011, 0.0, 2000),
                          ("Multilayer ceramic chip capacitors 10 uF 25 V, 0805 size, on reels", "85322400", "pcs", 0.018, 0.00002, 0.00004, 0.0, 50000)]},
    "steel": {"goods": ("Hot-rolled steel coils and zinc-plated bolts", "Kuumavalssatut teräskelat ja sinkityt pultit"), "seller": ("Anatolia Flat Steel Sanayi A.S. (sample)", "Organize Sanayi Bolgesi 4. Cadde No 12, Kocaeli", "TR",
                         "4780012345"), "currency": "EUR", "incoterm": ("CFR", "Kotka"), "origin_text": False,
              "lines": [("Hot-rolled non-alloy steel coils, width 1500 mm, thickness 2.5 mm, not clad or coated", "72083900", "t", 612.0, 1000.0, 1004.0, 0.0, 1),
                        ("Zinc-plated steel hexagon bolts M12x60 with nuts, grade 8.8", "73181588", "kg", 1.95, 1.0, 1.04, 3.7, 50)]},
    "outdoor": {"goods": ("Softshell jackets and hiking trousers", "Softshell-takit ja retkeilyhousut"), "seller": ("Saigon Outdoor Garments JSC (sample)", "12 Tan Thuan Export Processing Zone, District 7, Ho Chi Minh City", "VN",
                           "0301234567"), "currency": "USD", "incoterm": ("FOB", "Cat Lai, Ho Chi Minh City"), "origin_text": True,
                "lines": [("Men's softshell jackets of polyester woven fabric with fleece lining, hooded", "62014090", "pcs", 21.6, 0.62, 0.7, 12.0, 20),
                          ("Women's waterproof hiking trousers of polyamide woven fabric", "62046318", "pcs", 14.8, 0.38, 0.43, 12.0, 25)]},
    "marine": {"goods": ("Stainless steel boat propellers", "Ruostumattomat teräksiset veneen potkurit"), "seller": ("Gulf Coast Marine Hardware Inc. (sample)", "4410 Industrial Pkwy, Mobile, AL 36619", "US", "63-1234567"),
               "currency": "USD", "incoterm": ("FCA", "Houston"), "origin_text": False,
               "lines": [("Stainless steel three-blade boat propellers for outboard motors 40-140 hp", "84871090", "pcs", 289.0, 6.4, 7.3, 1.7, 1)]},
    "fashion": {"goods": ("Cotton T-shirts and knitted dresses", "Puuvillaiset T-paidat ja neulemekot"), "seller": ("Dhaka Knit Apparels Ltd. (sample)", "Plot 22, Dhaka Export Processing Zone, Savar, Dhaka", "BD", "BIN000123456"),
                "currency": "USD", "incoterm": ("FOB", "Chittagong"), "origin_text": True,
                "lines": [("Men's T-shirts of cotton jersey, knitted, crew neck, 180 g/m2", "61091000", "pcs", 3.85, 0.2, 0.22, 12.0, 50),
                          ("Women's dresses of cotton, knitted, short sleeve", "61044200", "pcs", 9.2, 0.28, 0.31, 12.0, 30)]},
}
# K6 (Pohjolan Kaluste, Shanghai) carries its own goods. Its hold is "the invoice lists 52 cartons, the B/L 48" with the
# packing list missing, so its documents say exactly that: invoice 52, B/L 48 (what the carrier holds), no packing list.
CATALOG["lighting"] = {
    "goods": ("LED furniture lamps and metal fittings (LCL)", "Huonekalujen LED-valaisimet ja metallikiinnikkeet (LCL)"),
    "seller": ("Shanghai Brightline Lighting Co., Ltd.", "88 Chuanqiao Road, Pudong, Shanghai 201206", "CN", "91310115MA1BRIGHT6"),
    "currency": "USD", "incoterm": ("FCA", "Shanghai"), "origin_text": False,
    "packages": {"invoice": 52, "transport": 48}, "packing_list": False,
    "lines": [("LED furniture lamps with aluminium housing and 12 V driver, model LUMO", "94054239", "pcs", 18.6, 0.85, 1.1, 4.7, 10),
              ("Steel furniture brackets and fittings, zinc plated, model FIX-40", "83024200", "pcs", 1.9, 0.18, 0.2, 2.7, 50)]}
SEED_KEY_CATALOG = {"K6": "lighting"}
PKG = {"kaluste": "cartons", "bike": "cartons", "coffee": "bags", "electro": "cartons", "steel": "coils",
       "outdoor": "cartons", "marine": "crates", "fashion": "cartons", "lighting": "cartons"}


def catalog_key(shipment: dict, company_key: str) -> str:
    """The catalogue a demo shipment's documents are generated from: its own goods where it has them, else its company's."""
    return SEED_KEY_CATALOG.get(shipment.get("seed_key") or "") or company_key


def pick_shipments(ships: list[dict], keys: dict[str, str], limit: int = 8) -> list[tuple[dict, str]]:
    """One import shipment per demo company, (shipment, catalogue key). A shipment whose origin country is the catalogue
    seller's country comes first, so the documents' goods and origin fit the shipment; otherwise the first one (as
    before). Order is by created_at, which is relative to seed time, so the choice must not depend on it for fit."""
    picked, seen = [], set()
    for match_only in (True, False):
        for s in ships:
            company_key = keys.get(s.get("company_id"))
            if s["company_id"] in seen or company_key not in CATALOG:
                continue
            key = catalog_key(s, company_key)
            if match_only and ((s.get("origin") or {}).get("country") or "").upper() != CATALOG[key]["seller"][2]:
                continue
            picked.append((s, key))
            seen.add(s["company_id"])
    order = {id(s): i for i, s in enumerate(ships)}
    return sorted(picked, key=lambda p: order[id(p[0])])[:limit]


# ---------------------------------------------------------------------------------------------
# PDF renderers (also used by tests)
# ---------------------------------------------------------------------------------------------
def _canvas_doc(title: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=title)
    return buf, doc


def _stamp(sample: bool):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4

    def draw(canvas, doc):
        if not sample:
            return
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#9a3412"))
        canvas.drawString(15 * 2.835, A4[1] - 10 * 2.835, SAMPLE)
        canvas.drawString(15 * 2.835, 8 * 2.835, SAMPLE)
        canvas.restoreState()
    return draw


def _styles():
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    ss = getSampleStyleSheet()
    return {"n": ParagraphStyle("n", parent=ss["Normal"], fontName="Helvetica", fontSize=8.5, leading=11),
            "s": ParagraphStyle("s", parent=ss["Normal"], fontName="Helvetica", fontSize=7.5, leading=9.5),
            "h": ParagraphStyle("h", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=18),
            "b": ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=8.5, leading=11)}


def render_invoice_pdf(inv: dict, sample: bool = True) -> bytes:
    """inv: number, date, seller{name,address,country,vat}, buyer{name,address,country,vat,eori}, incoterm, place,
    currency, lines[{description, hs, origin, qty, unit, unit_price, amount, net, gross}], freight, insurance,
    packages, package_type, gross, net, payment_terms, po, origin_statement"""
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    st = _styles()
    buf, doc = _canvas_doc(f"Commercial invoice {inv['number']}")
    s, b = inv["seller"], inv["buyer"]
    cur = inv["currency"]
    fmt = lambda v, d=2: f"{v:,.{d}f}"  # noqa: E731
    head = Table([[Paragraph(f"<b>{s['name']}</b><br/>{s['address']}<br/>{s['country_name']}<br/>Tax ID: {s['vat']}", st["n"]),
                   Paragraph("<font size=15><b>COMMERCIAL INVOICE</b></font><br/><br/>"
                             f"Invoice No.: <b>{inv['number']}</b><br/>Date: {inv['date']}<br/>"
                             f"Customer PO: {inv.get('po') or '-'}", st["n"])]], colWidths=[100 * mm, 80 * mm])
    parties = Table([[Paragraph("<b>Sold to / Buyer</b>", st["n"]), Paragraph("<b>Ship to / Consignee</b>", st["n"])],
                     [Paragraph(f"{b['name']}<br/>{b['address']}<br/>Finland<br/>VAT: {b['vat']}<br/>EORI: {b['eori']}", st["n"]),
                      Paragraph(f"{b['name']}<br/>{b['address']}<br/>Finland", st["n"])]], colWidths=[90 * mm, 90 * mm])
    parties.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, colors.grey), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                                 ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    terms = Table([[f"Terms of delivery: {inv['incoterm']} {inv['place']} (Incoterms 2020)", f"Currency: {cur}",
                    f"Payment: {inv.get('payment_terms') or '30 days net'}"]], colWidths=[85 * mm, 35 * mm, 60 * mm])
    terms.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 8)]))
    rows = [["#", "Description of goods", "HS code", "Origin", "Qty", "Unit", "Unit price", f"Amount {cur}"]]
    for i, ln in enumerate(inv["lines"], 1):
        rows.append([str(i), Paragraph(ln["description"], st["s"]), ln["hs"], ln["origin"], fmt(ln["qty"], 0 if ln["qty"] == int(ln["qty"]) else 3),
                     ln["unit"], fmt(ln["unit_price"], 4 if ln["unit_price"] < 1 else 2), fmt(ln["amount"])])
    sub = round(sum(ln["amount"] for ln in inv["lines"]), 2)
    rows.append(["", "", "", "", "", "", "Subtotal", fmt(sub)])
    total = sub
    if inv.get("freight"):
        rows.append(["", "", "", "", "", "", "Freight", fmt(inv["freight"])])
        total += inv["freight"]
    if inv.get("insurance"):
        rows.append(["", "", "", "", "", "", "Insurance", fmt(inv["insurance"])])
        total += inv["insurance"]
    rows.append(["", "", "", "", "", "", "TOTAL", fmt(round(total, 2))])
    t = Table(rows, colWidths=[7 * mm, 70 * mm, 19 * mm, 12 * mm, 17 * mm, 11 * mm, 20 * mm, 24 * mm], repeatRows=1)
    n_lines = len(inv["lines"])
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 7.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")), ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, n_lines), 0.25, colors.lightgrey),
                           ("FONT", (6, -1), (-1, -1), "Helvetica-Bold", 8)]))
    weights = Paragraph(f"Packages: {inv['packages']} {inv['package_type']} · Total net weight: {fmt(inv['net'], 1)} kg · "
                        f"Total gross weight: {fmt(inv['gross'], 1)} kg", st["n"])
    story = [head, Spacer(1, 6), parties, Spacer(1, 4), terms, Spacer(1, 4), t, Spacer(1, 6), weights, Spacer(1, 6)]
    if inv.get("origin_statement"):
        story += [Paragraph(f"<b>Statement on origin</b>: {inv['origin_statement']}", st["s"]), Spacer(1, 6)]
    story += [Paragraph("We certify that this invoice is true and correct and that the goods are of the origin stated.", st["s"]),
              Spacer(1, 14), Paragraph(f"For and on behalf of {s['name']} — Authorised signature", st["s"])]
    doc.build(story, onFirstPage=_stamp(sample), onLaterPages=_stamp(sample))
    return buf.getvalue()


def render_packing_list_pdf(pl: dict, sample: bool = True) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    st = _styles()
    buf, doc = _canvas_doc(f"Packing list {pl['number']}")
    fmt = lambda v, d=1: f"{v:,.{d}f}"  # noqa: E731
    head = Table([[Paragraph(f"<b>{pl['shipper']['name']}</b><br/>{pl['shipper']['address']}<br/>{pl['shipper']['country_name']}", st["n"]),
                   Paragraph(f"<font size=15><b>PACKING LIST</b></font><br/><br/>No.: <b>{pl['number']}</b><br/>Date: {pl['date']}<br/>"
                             f"Invoice No.: {pl['invoice_number']}", st["n"])]], colWidths=[100 * mm, 80 * mm])
    cons = Paragraph(f"<b>Consignee</b><br/>{pl['consignee']['name']}<br/>{pl['consignee']['address']}<br/>Finland", st["n"])
    rows = [["#", "Description", "Packages", "Quantity", "Net kg", "Gross kg", "Measurements"]]
    for i, ln in enumerate(pl["lines"], 1):
        rows.append([str(i), Paragraph(ln["description"], st["s"]), f"{ln['packages']} {ln['package_type']}",
                     f"{ln['qty']:,.0f} {ln['unit']}" if ln["qty"] == int(ln["qty"]) else f"{ln['qty']:,.3f} {ln['unit']}",
                     fmt(ln["net"]), fmt(ln["gross"]), ln.get("dims") or "-"])
    rows.append(["", "TOTAL", f"{pl['packages']} {pl['package_type']}", "", fmt(pl["net"]), fmt(pl["gross"]), f"{pl['volume']:.2f} m3"])
    t = Table(rows, colWidths=[7 * mm, 68 * mm, 24 * mm, 25 * mm, 17 * mm, 18 * mm, 21 * mm], repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 7.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                           ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 7.5)]))
    extra = []
    if pl.get("containers"):
        extra.append(f"Container(s): {', '.join(pl['containers'])}" + (f" · Seal: {', '.join(pl['seals'])}" if pl.get("seals") else ""))
    extra.append(f"Marks and numbers: {pl.get('marks') or 'N/M'}")
    story = [head, Spacer(1, 6), cons, Spacer(1, 6), t, Spacer(1, 6)] + [Paragraph(x, st["n"]) for x in extra]
    doc.build(story, onFirstPage=_stamp(sample), onLaterPages=_stamp(sample))
    return buf.getvalue()


def render_bl_pdf(bl: dict, sample: bool = True) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    st = _styles()
    buf, doc = _canvas_doc(f"Bill of lading {bl['number']}")
    cell = lambda title, body: Paragraph(f"<font size=6.5 color='#555555'>{title}</font><br/>{body}", st["n"])  # noqa: E731
    grid = [[cell("Shipper", f"{bl['shipper']['name']}<br/>{bl['shipper']['address']}"),
             cell("Bill of lading No.", f"<b>{bl['number']}</b><br/>{bl['carrier']} · Non-negotiable copy")],
            [cell("Consignee", f"{bl['consignee']['name']}<br/>{bl['consignee']['address']}, Finland"),
             cell("Notify party", "Same as consignee")],
            [cell("Vessel / voyage", f"{bl['vessel']} / {bl['voyage']}"), cell("Port of loading", bl["pol"])],
            [cell("Port of discharge", bl["pod"]), cell("Place of delivery", bl.get("delivery") or bl["pod"])]]
    g = Table(grid, colWidths=[90 * mm, 90 * mm])
    g.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    goods = Table([["Container / seal", "No. of packages", "Description of goods", "Gross weight", "Measurement"],
                   [Paragraph("<br/>".join(f"{c} / {s}" for c, s in zip(bl["containers"], bl["seals"])) or "-", st["s"]),
                    f"{bl['packages']} {bl['package_type']}", Paragraph(f"SAID TO CONTAIN: {bl['description']}", st["s"]),
                    f"{bl['gross']:,.1f} KGS", f"{bl['volume']:.2f} CBM"]],
                  colWidths=[40 * mm, 27 * mm, 66 * mm, 25 * mm, 22 * mm])
    goods.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 7.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7),
                               ("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story = [Paragraph("BILL OF LADING", st["h"]), Spacer(1, 4), g, Spacer(1, 6), goods, Spacer(1, 6),
             Paragraph(f"Freight {bl.get('freight_terms', 'collect')}. Shipped on board {bl['on_board']}. Place and date of issue: "
                       f"{bl['pol']}, {bl['on_board']}. Number of original B/Ls: 3.", st["n"])]
    doc.build(story, onFirstPage=_stamp(sample), onLaterPages=_stamp(sample))
    return buf.getvalue()


COUNTRY_NAMES = {"VN": "Viet Nam", "TW": "Taiwan", "BR": "Brazil", "CN": "China", "TR": "Türkiye", "US": "United States",
                 "BD": "Bangladesh", "IN": "India", "JP": "Japan", "KR": "Korea"}


def _container_no(rnd: random.Random, owner: str = "MSKU") -> str:
    from services.extraction import iso6346_ok

    for _ in range(50):
        body = f"{owner}{rnd.randint(1000000, 9999999)}"[:10]
        for d in range(10):
            cn = body + str(d)
            if iso6346_ok(cn):
                return cn
    return owner + "0000000"


def _mrn(rnd: random.Random) -> str:
    """Demo MRN: 26FI + DEMO + 8 digits + R (import declaration) + ISO 6346 check digit."""
    from services.customs import mrn_check_digit

    base = f"{now().strftime('%y')}FIDEMO{rnd.randint(10000000, 99999999)}R"
    return base + mrn_check_digit(base)


def build_documents_data(shipment: dict, company: dict, key: str, rnd: random.Random) -> dict:
    cat = CATALOG[key]
    gross_target = float((shipment.get("cargo") or {}).get("weight_kg") or 0) or None
    lines = []
    for desc, hs, unit, price, unit_net, unit_gross, duty, step in cat["lines"]:
        lines.append({"description": desc, "hs": hs, "unit": unit, "unit_price": price, "unit_net": unit_net,
                      "unit_gross": unit_gross, "duty": duty, "step": step})
    # quantities that roughly reproduce the booked gross weight
    if gross_target:
        share = [1 / len(lines)] * len(lines)
        for ln, sh in zip(lines, share):
            q = max(ln["step"], round(gross_target * sh / ln["unit_gross"] / ln["step"]) * ln["step"])
            ln["qty"] = float(q)
    else:
        for ln in lines:
            ln["qty"] = float(ln["step"] * rnd.randint(10, 60))
    seller_name, seller_addr, seller_cc, seller_vat = cat["seller"]
    origin = seller_cc
    for ln in lines:
        ln["origin"] = origin
        ln["amount"] = round(ln["qty"] * ln["unit_price"], 2)
        ln["net"] = round(ln["qty"] * ln["unit_net"], 1)
        ln["gross"] = round(ln["qty"] * ln["unit_gross"], 1)
    gross = round(sum(ln["gross"] for ln in lines), 1)
    net = round(sum(ln["net"] for ln in lines), 1)
    ptype = PKG.get(key, "cartons")
    if ptype == "coils":
        pk_per = [max(1, int(ln["qty"])) if ln["unit"] == "t" else max(1, int(ln["gross"] // 500) + 1) for ln in lines]
    elif ptype == "bags":
        pk_per = [int(ln["qty"] // 60) for ln in lines]
    else:
        pk_per = [max(1, int(round(ln["gross"] / 18))) for ln in lines]
    fixed = cat.get("packages") or {}
    if fixed.get("invoice"):
        weights = [ln["gross"] for ln in lines]
        pk_per = [int(fixed["invoice"] * w // sum(weights)) for w in weights]
        pk_per[0] += fixed["invoice"] - sum(pk_per)
    packages = sum(pk_per)
    transport_packages = int(fixed.get("transport") or packages)
    a = company.get("address") or {}
    buyer = {"name": company["name"], "address": f"{a.get('street')}, {a.get('postal_code')} {a.get('city')}",
             "country": "FI", "vat": company.get("vat_id"), "eori": company.get("eori")}
    created = parse_iso(shipment.get("created_at")) or now()
    inv_date = (created - timedelta(days=rnd.randint(2, 9))).date().isoformat()
    inc, place = cat["incoterm"]
    inv_no = f"{seller_name.split()[0][:3].upper()}-{created.strftime('%y')}{rnd.randint(1000, 9999)}"
    origin_statement = None
    if cat.get("origin_text"):
        origin_statement = (f"The exporter of the products covered by this document (REX No. {seller_cc}REX{rnd.randint(100000, 999999)}) "
                            f"declares that, except where otherwise clearly indicated, these products are of "
                            f"{COUNTRY_NAMES.get(seller_cc, seller_cc)} preferential origin.")
    containers = []
    if shipment.get("mode") == "sea" and ((shipment.get("cargo") or {}).get("containers")):
        n = sum(int(c.get("count") or 1) for c in shipment["cargo"]["containers"]) or 1
        containers = [_container_no(rnd, rnd.choice(["MSKU", "TGHU", "CMAU", "HLXU"])) for _ in range(min(n, 3))]
    seals = [f"SL{rnd.randint(100000, 999999)}" for _ in containers]
    return {
        "invoice": {"number": inv_no, "date": inv_date, "po": f"PO-{rnd.randint(20000, 29999)}",
                    "seller": {"name": seller_name, "address": seller_addr, "country": seller_cc,
                               "country_name": COUNTRY_NAMES.get(seller_cc, seller_cc), "vat": seller_vat},
                    "buyer": buyer, "incoterm": inc, "place": place, "currency": cat["currency"], "lines": lines,
                    "packages": packages, "package_type": ptype, "gross": gross, "net": net,
                    "payment_terms": "30 days net from invoice date", "origin_statement": origin_statement},
        "packing": {"number": f"PL-{inv_no}", "date": inv_date, "invoice_number": inv_no,
                    "shipper": {"name": seller_name, "address": seller_addr, "country_name": COUNTRY_NAMES.get(seller_cc, seller_cc)},
                    "consignee": buyer, "lines": [{**ln, "packages": p, "package_type": ptype} for ln, p in zip(lines, pk_per)],
                    "packages": packages, "package_type": ptype, "gross": gross, "net": net,
                    "volume": round(float((shipment.get("cargo") or {}).get("volume_m3") or gross / 300), 2),
                    "containers": containers, "seals": seals, "marks": f"{company['name'].split()[0].upper()} / {inv_no} / 1-{packages}"},
        "duty": {ln["hs"]: ln["duty"] for ln in lines},
        "transport_packages": transport_packages, "packing_list": cat.get("packing_list", True),
        "goods": cat.get("goods"), "hs_hint": lines[0]["hs"][:4] if lines else None,
    }


def _fields(pairs: dict) -> dict:
    return {k: {"value": v, "confidence": None, "source": "seed"} for k, v in pairs.items() if v not in (None, "", [])}


async def _make_doc(shipment, company, doc_type, filename, pdf, fields, line_items):
    from services.documents import create_document

    doc = await create_document(company_id=company["id"], shipment_id=shipment["id"], doc_type=doc_type, filename=filename,
                                data=pdf, actor=agent_actor("demo_seed"), source="seed", extract=False,
                                extra={"demo": True, "type_source": "seed"})
    ex = {"status": "done", "method": "seed", "source": "seed", "model": None, "fields": fields, "line_items": line_items,
          "warnings": [], "confidence": None, "completed_at": now_iso(), "attempts": 0,
          "reason": None, "note": "Values the sample PDF was generated from; no AI was run on this document"}
    num = (fields.get("invoice_number") or fields.get("packing_list_number") or fields.get("bl_number") or {}).get("value")
    await db.documents.update_one({"id": doc["id"]}, {"$set": {"extraction": ex, "number": num}})
    return doc["id"]


async def _ensure_demo_poa(company: dict):
    from services.documents import create_document
    from services.poa_pdf import consent_text, evidence_hash, render_poa_pdf

    if (company.get("poa") or {}).get("signed_at"):
        return company
    today = now().date()
    poa = {"type": company.get("representation_default") or "direct", "scope": ["import", "export", "correspondence"],
           "valid_from": (today - timedelta(days=30)).isoformat(), "valid_to": (today + timedelta(days=335)).isoformat(),
           "signature": {"typed_name": "Asiakkaan edustaja", "title": "Toimitusjohtaja", "email": "customer.demo@huolitsija.fi",
                         "user_id": None, "signed_at": now_iso(), "ip": None, "user_agent": "seed",
                         "consent_text": consent_text(company.get("representation_default") or "direct", company["name"])},
           "text_version": "poa-2026.1", "signed_at": now_iso(), "revoked_at": None, "demo": True}
    poa["evidence_hash"] = evidence_hash(company, poa)
    pdf = render_poa_pdf(company, poa)
    doc = await create_document(company_id=company["id"], shipment_id=None, doc_type="power_of_attorney",
                                filename=f"Valtakirja-{company.get('business_id')}.pdf", data=pdf,
                                actor=agent_actor("demo_seed"), source="seed", extract=False, extra={"demo": True})
    poa["document_id"] = doc["id"]
    await db.companies.update_one({"id": company["id"]}, {"$set": {"poa": poa}})
    return await db.companies.find_one({"id": company["id"]}, NO_ID)


def cargo_from_documents(data: dict) -> dict:
    """Shipment cargo fields that must agree with the seeded documents: goods, the carrier's package count (B/L), weights."""
    inv = data["invoice"]
    out = {"packages": data["transport_packages"], "package_type": inv["package_type"], "weight_kg": inv["gross"],
           "net_weight_kg": inv["net"]}
    if data.get("goods"):
        out.update(description=data["goods"][0], description_i18n={"en": data["goods"][0], "fi": data["goods"][1]},
                   hs_hint=data.get("hs_hint"))
    return out


async def seed_shipment(shipment: dict, company: dict, key: str, target: str, rnd: random.Random) -> dict | None:
    from services import customs as svc

    if shipment.get("status") == "customs_hold":
        target = "draft"
    data = build_documents_data(shipment, company, key, rnd)
    inv, pl = data["invoice"], data["packing"]
    cargo = {**(shipment.get("cargo") or {}), **cargo_from_documents(data)}
    await db.shipments.update_one({"id": shipment["id"]}, {"$set": {"cargo": cargo}})
    shipment["cargo"] = cargo
    inv_pdf = render_invoice_pdf(inv)
    pl_pdf = render_packing_list_pdf(pl)
    inv_fields = _fields({
        "invoice_number": inv["number"], "invoice_date": inv["date"], "seller_name": inv["seller"]["name"],
        "seller_address": inv["seller"]["address"], "seller_country": inv["seller"]["country"],
        "seller_vat_id": inv["seller"]["vat"], "buyer_name": inv["buyer"]["name"], "buyer_address": inv["buyer"]["address"],
        "buyer_country": "FI", "buyer_vat_id": inv["buyer"]["vat"], "buyer_eori": inv["buyer"]["eori"],
        "incoterm": inv["incoterm"], "incoterm_place": inv["place"], "currency": inv["currency"],
        "subtotal_amount": round(sum(ln["amount"] for ln in inv["lines"]), 2),
        "total_amount": round(sum(ln["amount"] for ln in inv["lines"]), 2), "payment_terms": inv["payment_terms"],
        "country_of_origin": inv["seller"]["country"], "total_packages": inv["packages"],
        "total_gross_weight_kg": inv["gross"], "total_net_weight_kg": inv["net"], "purchase_order_number": inv["po"],
        "origin_declaration_text": inv.get("origin_statement")})
    inv_items = [{"description": ln["description"], "quantity": ln["qty"], "unit": ln["unit"], "unit_price": ln["unit_price"],
                  "amount": ln["amount"], "hs_code": ln["hs"], "country_of_origin": ln["origin"], "net_weight_kg": ln["net"],
                  "gross_weight_kg": ln["gross"], "confidence": None} for ln in inv["lines"]]
    await _make_doc(shipment, company, "commercial_invoice", f"Commercial-invoice-{inv['number']}.pdf", inv_pdf, inv_fields, inv_items)
    pl_fields = _fields({"packing_list_number": pl["number"], "date": pl["date"], "invoice_number": pl["invoice_number"],
                         "shipper_name": pl["shipper"]["name"], "consignee_name": pl["consignee"]["name"],
                         "consignee_address": pl["consignee"]["address"], "total_packages": pl["packages"],
                         "package_type": pl["package_type"], "total_gross_weight_kg": pl["gross"],
                         "total_net_weight_kg": pl["net"], "total_volume_m3": pl["volume"],
                         "container_numbers": pl["containers"], "seal_numbers": pl["seals"], "marks_and_numbers": pl["marks"]})
    pl_items = [{"description": ln["description"], "packages": ln["packages"], "package_type": ln["package_type"],
                 "quantity": ln["qty"], "net_weight_kg": ln["net"], "gross_weight_kg": ln["gross"], "confidence": None}
                for ln in pl["lines"]]
    if data["packing_list"]:
        await _make_doc(shipment, company, "packing_list", f"Packing-list-{pl['number']}.pdf", pl_pdf, pl_fields, pl_items)
    if shipment.get("mode") == "sea":
        leg = next((lg for lg in shipment.get("legs") or [] if lg.get("mode") == "sea"), {})
        bl_no = leg.get("transport_doc_no") or f"{rnd.choice(['MAEU', 'HLCU', 'CMDU'])}{rnd.randint(100000000, 999999999)}"
        bl = {"number": bl_no, "carrier": leg.get("carrier") or "Ocean carrier", "shipper": pl["shipper"], "consignee": pl["consignee"],
              "vessel": (leg.get("vessel") or {}).get("name") or "—", "voyage": leg.get("voyage") or "—",
              "pol": (leg.get("from") or {}).get("name") or (shipment.get("origin") or {}).get("name") or "—",
              "pod": (leg.get("to") or {}).get("name") or (shipment.get("destination") or {}).get("name") or "—",
              "containers": pl["containers"], "seals": pl["seals"], "packages": data["transport_packages"], "package_type": pl["package_type"],
              "description": "; ".join(ln["description"] for ln in inv["lines"])[:300], "gross": pl["gross"], "volume": pl["volume"],
              "on_board": (parse_iso(leg.get("atd") or leg.get("etd") or shipment.get("etd")) or now()).date().isoformat(),
              "freight_terms": "prepaid" if inv["incoterm"] in ("CFR", "CIF", "CPT", "CIP") else "collect"}
        bl_fields = _fields({"bl_number": bl_no, "bl_kind": "original", "shipper_name": bl["shipper"]["name"],
                             "consignee_name": bl["consignee"]["name"], "carrier_name": bl["carrier"], "vessel_name": bl["vessel"],
                             "voyage_number": bl["voyage"], "port_of_loading": bl["pol"], "port_of_discharge": bl["pod"],
                             "container_numbers": bl["containers"], "seal_numbers": bl["seals"], "total_packages": bl["packages"],
                             "package_type": bl["package_type"], "total_gross_weight_kg": bl["gross"], "total_volume_m3": bl["volume"],
                             "freight_terms": bl["freight_terms"], "number_of_originals": 3, "shipped_on_board_date": bl["on_board"]})
        await _make_doc(shipment, company, "bill_of_lading", f"BL-{bl_no}.pdf", render_bl_pdf(bl), bl_fields, [])
    from services.documents import run_tieout

    await run_tieout(shipment["id"])
    decl = await svc.draft_declaration_for_shipment(shipment["id"], agent_actor("customs_drafter"), classify=False)
    # demo duty values (labelled), seller screening
    for it in decl["items"]:
        rate = data["duty"].get((it.get("hs_hint") or "")[:8])
        if rate is not None:
            it["duty_rate_pct"] = rate
            it["duty_source"] = "seed"
            it["duty_label"] = "Demo value from the seed — verify in TARIC"
    from services import screening

    if any(x.get("loaded") for x in await screening.lists_status()):  # never block startup on a first list download
        decl["screening"] = await svc.screen_seller(decl)
    decl["demo"] = True
    office = {"code": "FI002000", "name": "Finnish Customs — verify the office of presentation"}
    port = (shipment.get("destination") or {}).get("name") or "Vuosaari"
    if target != "draft":
        decl["office"] = office
        decl["location_of_goods"] = f"{port} terminal"
        for it in decl["items"]:
            if it.get("cn_code"):
                it["cn_confirmed"] = True
                it["cn_confirmed_by"] = {"type": "system", "name": "Järjestelmä"}
                it["cn_confirmed_at"] = now_iso()
    if target != "draft":
        demo_freight(decl, shipment)
    decl = await svc.save_and_recompute(decl, agent_actor("demo_seed"), "seeded")
    actor_cust = {"type": "user", "id": None, "name": "Asiakkaan edustaja", "org_type": "customer"}
    actor_ops = {"type": "user", "id": None, "name": "Huolitsija", "org_type": "internal"}
    if target in ("ready_for_review", "submitted", "released") and decl["status"] in ("draft", "needs_info"):
        decl = await svc.transition(decl, "ready_for_review", actor_cust, "Goods data confirmed", force=True,
                                    extra={"confirmation": {"by": actor_cust, "at": now_iso()}})
    if target in ("submitted", "released") and not svc.blocking(decl):
        decl = await svc.transition(decl, "approved", actor_ops, "Filing approved", force=True,
                                    extra={"approved_by": actor_ops, "approved_at": now_iso()})
        decl = await svc.transition(decl, "submitted", actor_ops, "Filed in Tulli's Customs Clearance Service", force=True,
                                    extra={"submission": {"channel": "manual", "state": "filed_manually", "submitted_at": now_iso(),
                                                          "messages": []}})
    if target == "released" and decl["status"] == "submitted":
        mrn = _mrn(rnd)
        decl = await svc.transition(decl, "accepted", actor_ops, f"Accepted, MRN {mrn}", {"mrn": mrn, "accepted_at": now_iso()})
        decl = await svc.transition(decl, "released", actor_ops, "Released for free circulation",
                                    {"released_at": now_iso()})
    return decl


def demo_freight(decl: dict, shipment: dict) -> dict:
    """V1's demo shipments have no V2 quote, so carriage to the EU border is unknown (RULE I-5). For demo declarations
    past the draft stage the seed enters typical values and says so in the valuation notes."""
    v = decl.setdefault("valuation", {})
    if v.get("freight_known"):
        return decl
    cargo = shipment.get("cargo") or {}
    mode = shipment.get("mode")
    if mode == "sea":
        n = sum(int(c.get("count") or 1) for c in cargo.get("containers") or [])
        freight = n * 185000 if n else int(round(float(cargo.get("volume_m3") or 5) * 9500))
    elif mode == "air":
        kg = max(float(cargo.get("weight_kg") or 100), float(cargo.get("volume_m3") or 0) * 167)
        freight = int(round(kg * 380))
    elif mode == "rail":
        freight = 210000
    else:
        freight = 120000
    group = v.get("incoterm_group")
    inv_eur = sum(int(it.get("value_cents") or 0) for it in decl.get("items") or [])
    if group in ("E", "F"):
        v["freight_to_border_cents"] = freight
    if group in ("E", "F") or v.get("incoterm") in ("CFR", "CPT"):
        v["insurance_cents"] = max(2500, int(round(inv_eur * 0.003)))
    v["freight_known"] = True
    v.setdefault("notes", []).append("Freight and insurance are valuation estimates; verify them against transport invoices.")
    return decl


@on_startup(order=40)
async def seed_customs():
    if not config.SEED_DEMO or await db.system.find_one({"key": MARKER}):
        return
    from services.seed_core import DEMO_COMPANIES, demo_company_id

    keys = {demo_company_id(c["key"]): c["key"] for c in DEMO_COMPANIES}
    ships = await db.shipments.find({"demo": True, "direction": "import", "company_id": {"$in": list(keys)}}, NO_ID) \
        .sort("created_at", 1).to_list(200)
    from services.documents import EU_COUNTRIES

    ships = [s for s in ships if ((s.get("origin") or {}).get("country") or "").upper() not in EU_COUNTRIES
             and not await db.documents.find_one({"shipment_id": s["id"], "type": "commercial_invoice"})]
    if not ships:
        log.info("customs seed: no V1 demo import shipments yet; will retry on next start")
        return
    rnd = random.Random(20261024)
    targets = ["released", "submitted", "ready_for_review", "draft", "released", "draft", "submitted", "ready_for_review"]
    done = 0
    for (s, key), target in zip(pick_shipments(ships, keys), targets):
        try:
            company = await db.companies.find_one({"id": s["company_id"]}, NO_ID)
            company = await _ensure_demo_poa(company)
            await seed_shipment(s, company, key, target, rnd)
            done += 1
        except Exception:  # noqa: BLE001
            log.exception("customs seed failed for shipment %s", s.get("reference"))
    if done:
        await db.system.update_one({"key": MARKER}, {"$set": {"at": now_iso(), "declarations": done}}, upsert=True)
        log.info("customs seed: %d demo declarations", done)
