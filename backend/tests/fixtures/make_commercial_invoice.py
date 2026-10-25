"""Builds the commercial invoice used by the E2E smoke test (supplier in China -> Finnish importer, FOB Yantian).
Run: python make_commercial_invoice.py <out.pdf>"""
import os
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

ITEMS = [
    # description, model, qty, unit, unit price USD, net kg, gross kg, origin
    ("Bicycle rear derailleur, 11-speed, aluminium/steel", "RD-M7100-SGS", 400, "pcs", 18.40, 112.0, 128.0, "CN"),
    ("Bicycle chain, 11-speed, nickel-plated steel, 116 links", "CN-HG601", 1200, "pcs", 6.10, 307.2, 330.0, "CN"),
    ("LED bicycle front light, USB rechargeable, 800 lm", "FL-800R", 600, "pcs", 9.75, 102.0, 126.0, "CN"),
    ("Hydraulic disc brake set, front+rear", "BR-MT420", 250, "sets", 31.20, 210.0, 236.0, "CN"),
]

def main(out: str) -> None:
    ss = getSampleStyleSheet()
    small = ss["BodyText"].clone("small", fontSize=8, leading=10)
    h = ss["Heading2"].clone("h", fontSize=14)
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title="Commercial Invoice SZ-CI-2026-10417", author="Shenzhen Velotek Components Co., Ltd.")
    story = [Paragraph("COMMERCIAL INVOICE", h)]
    head = [
        [Paragraph("<b>Seller / Exporter</b><br/>Shenzhen Velotek Components Co., Ltd.<br/>No. 18 Huanguan South Road, Guanlan, Longhua District<br/>Shenzhen 518110, Guangdong, China<br/>USCC 91440300MA5F8K2Q3X", small),
         Paragraph("<b>Invoice No.</b> SZ-CI-2026-10417<br/><b>Date</b> 2026-10-20<br/><b>Order No.</b> NBP-PO-2026-088<br/><b>Terms of delivery</b> FOB Yantian (Incoterms 2020)<br/><b>Payment</b> T/T 30% deposit, 70% against B/L copy<br/><b>Currency</b> USD", small)],
        [Paragraph("<b>Buyer / Importer (consignee)</b><br/>%s<br/>%s<br/>VAT %s · EORI %s" % (
            os.environ.get("CONSIGNEE_NAME", "Nordic Bike Parts Oy"), os.environ.get("CONSIGNEE_ADDRESS", "Teollisuuskatu 21<br/>00510 Helsinki, Finland"),
            os.environ.get("CONSIGNEE_VAT", "FI39900022"), os.environ.get("CONSIGNEE_EORI", "FI3990002-2")), small),
         Paragraph("<b>Port of loading</b> Yantian, CN (CNYTN)<br/><b>Port of discharge</b> Helsinki-Vuosaari, FI (FIHEL)<br/><b>Mode</b> Sea, LCL<br/><b>Country of origin</b> China<br/><b>Marks</b> NBP / HELSINKI / C/NO. 1-58", small)],
    ]
    t = Table(head, colWidths=[95 * mm, 85 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                           ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [t, Spacer(1, 6 * mm)]
    rows = [["#", "Description of goods", "Model", "Qty", "Unit", "Unit price", "Amount", "Net kg", "Gross kg", "Origin"]]
    total = net = gross = 0.0
    for i, (d, m, q, u, p, n, g, o) in enumerate(ITEMS, 1):
        amt = round(q * p, 2)
        total += amt; net += n; gross += g
        rows.append([str(i), Paragraph(d, small), m, f"{q:,}", u, f"{p:,.2f}", f"{amt:,.2f}", f"{n:,.1f}", f"{g:,.1f}", o])
    rows.append(["", Paragraph("<b>Total FOB Yantian</b>", small), "", f"{sum(i[2] for i in ITEMS):,}", "", "", f"USD {total:,.2f}", f"{net:,.1f}", f"{gross:,.1f}", ""])
    it = Table(rows, colWidths=[7 * mm, 52 * mm, 22 * mm, 13 * mm, 10 * mm, 16 * mm, 22 * mm, 13 * mm, 15 * mm, 10 * mm], repeatRows=1)
    it.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7.5), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("ALIGN", (3, 1), (-2, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [it, Spacer(1, 5 * mm)]
    story.append(Paragraph(
        "Packages: 58 cartons on 4 pallets · Total volume 6.4 m³ · Total gross weight %.1f kg · Total net weight %.1f kg<br/>"
        "Amount in words: US Dollars %s only.<br/><br/>"
        "We hereby certify that this invoice shows the actual price of the goods described, that all particulars are true and correct, "
        "and that the goods are of Chinese origin.<br/><br/>For and on behalf of Shenzhen Velotek Components Co., Ltd.<br/>Authorised signature: Li Wei, Export Manager"
        % (gross, net, f"{total:,.2f}"), small))
    doc.build(story)
    print(f"{out}: USD {total:,.2f}, net {net} kg, gross {gross} kg, {len(ITEMS)} lines")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "commercial_invoice_velotek.pdf")
