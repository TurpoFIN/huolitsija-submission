"""Make realistic carrier purchase invoices for a shipment: a text-layout PDF (reportlab, no images) and a Finvoice 3.0
XML message validated against Finanssiala's official XSD (backend/data/finvoice/Finvoice3.0.xsd).

Used by backend/tests/test_s13_ap_*.py (import make_pdf / make_finvoice / invoice_dict) and from the command line:

    /home/user/work/venv/bin/python scripts/s13_make_carrier_invoices.py --shipment-ref HUO-2026-000123 \
        --out /home/user/work/scratch --seller "Testivarustamo Oy" --vat-id FI12345678 --iban FI2112345600000785 \
        --line "Merirahti 1x40HC:1900.00:0" --line "Jakelu Vuosaari-Lahti:300.00:25.5"

Amounts are invoice-currency decimals; VAT is computed per line, half-up to the cent.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from services.finvoice import build_finvoice, validate_finvoice  # noqa: E402

BUYER = {"name": "Huolitsija Oy", "business_id": "3456789-0", "vat_id": "FI34567890", "street": "Satamakaari 24",
         "postal_code": "00980", "city": "Helsinki", "country": "FI"}


def vat_cents(net_cents: int, rate_pct: float) -> int:
    return int((Decimal(net_cents) * Decimal(str(rate_pct)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def invoice_dict(*, number: str, seller: dict, rows: list[dict], invoice_date: str | None = None, due_days: int = 14,
                 currency: str = "EUR", reference: str | None = None, type_code: str = "INV01",
                 original_invoice_number: str | None = None, free_text: list[str] | None = None) -> dict:
    """rows: [{name, amount_cents (net), vat_rate_pct, free_text[]?, quantity?}] -> dict for build_finvoice/make_pdf."""
    idate = invoice_date or date.today().isoformat()
    out_rows = []
    for r in rows:
        q = r.get("quantity") or 1
        out_rows.append({**r, "quantity": q, "unit_price_cents": r.get("unit_price_cents", int(round(r["amount_cents"] / q))),
                         "vat_cents": r.get("vat_cents", vat_cents(r["amount_cents"], r["vat_rate_pct"]))})
    return {"number": number, "invoice_date": idate,
            "due_date": (date.fromisoformat(idate) + timedelta(days=due_days)).isoformat(), "currency": currency,
            "reference": reference, "type_code": type_code, "original_invoice_number": original_invoice_number,
            "seller": seller, "buyer": BUYER, "rows": out_rows, "free_text": free_text or []}


def make_finvoice(inv: dict, *, validate: bool = True) -> bytes:
    xml = build_finvoice(inv)
    if validate:
        v = validate_finvoice(xml)
        if not v["valid"]:
            raise ValueError("Generated Finvoice fails the official XSD: " + "; ".join(v["errors"]))
    return xml


def _money(c: int, cur: str) -> str:
    s = f"{abs(c) / 100:,.2f}".replace(",", " ").replace(".", ",")
    return f"{'-' if c < 0 else ''}{s} {cur}"


def make_pdf(inv: dict) -> bytes:
    """A plain carrier invoice PDF with a real text layer (the layout carriers' ERP systems print)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")
    w, h = A4
    s, b, cur = inv["seller"], inv["buyer"], inv.get("currency") or "EUR"
    y = h - 20 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, s["name"])
    c.setFont("Helvetica", 9)
    for line in [s.get("street") or "Satamakatu 1", f"{s.get('postal_code') or '00100'} {s.get('city') or 'Helsinki'}",
                 f"Y-tunnus / Business ID {s.get('business_id') or '-'}", f"ALV-tunnus / VAT ID {s.get('vat_id') or '-'}"]:
        y -= 4.5 * mm
        c.drawString(20 * mm, y, line)
    c.setFont("Helvetica-Bold", 16)
    title = "HYVITYSLASKU / CREDIT NOTE" if inv.get("type_code") == "INV02" else "LASKU / INVOICE"
    c.drawRightString(w - 20 * mm, h - 20 * mm, title)
    c.setFont("Helvetica", 9)
    meta = [("Laskun numero / Invoice no", inv["number"]), ("Laskun päivä / Invoice date", inv["invoice_date"]),
            ("Eräpäivä / Due date", inv["due_date"]), ("Viite / Reference", inv.get("reference") or "-"),
            ("Valuutta / Currency", cur)]
    if inv.get("original_invoice_number"):
        meta.append(("Hyvitettävä lasku / Credited invoice", inv["original_invoice_number"]))
    yy = h - 28 * mm
    for k, v in meta:
        c.drawRightString(w - 60 * mm, yy, k)
        c.drawString(w - 57 * mm, yy, str(v))
        yy -= 4.5 * mm
    y = min(y, yy) - 10 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Laskutettava / Bill to")
    c.setFont("Helvetica", 9)
    for line in [b["name"], b.get("street") or "", f"{b.get('postal_code') or ''} {b.get('city') or ''}",
                 f"VAT ID {b.get('vat_id') or '-'}"]:
        y -= 4.5 * mm
        c.drawString(20 * mm, y, line)
    y -= 12 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Kuvaus / Description")
    c.drawRightString(135 * mm, y, "Veroton / Net")
    c.drawRightString(155 * mm, y, "ALV %")
    c.drawRightString(w - 20 * mm, y, "ALV / VAT")
    c.line(20 * mm, y - 2 * mm, w - 20 * mm, y - 2 * mm)
    c.setFont("Helvetica", 9)
    for r in inv["rows"]:
        y -= 6 * mm
        c.drawString(20 * mm, y, r["name"][:60])
        c.drawRightString(135 * mm, y, _money(r["amount_cents"], cur))
        c.drawRightString(155 * mm, y, f"{r['vat_rate_pct']:g}".replace(".", ","))
        c.drawRightString(w - 20 * mm, y, _money(r["vat_cents"], cur))
        for t in r.get("free_text") or []:
            y -= 4.5 * mm
            c.drawString(24 * mm, y, t[:90])
    net = sum(r["amount_cents"] for r in inv["rows"])
    vat = sum(r["vat_cents"] for r in inv["rows"])
    y -= 10 * mm
    for k, v in [("Veroton yhteensä / Total excl. VAT", net), ("ALV yhteensä / VAT total", vat),
                 ("Maksettava / Total due", net + vat)]:
        c.drawRightString(155 * mm, y, k)
        c.drawRightString(w - 20 * mm, y, _money(v, cur))
        y -= 5 * mm
    for t in inv.get("free_text") or []:
        y -= 5 * mm
        c.drawString(20 * mm, y, t[:100])
    y -= 10 * mm
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y, f"IBAN {s['iban']}   BIC {s['bic']}   Viite / Reference {inv.get('reference') or '-'}")
    c.showPage()
    c.save()
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shipment-ref", required=True)
    ap.add_argument("--out", required=True, help="output directory (created)")
    ap.add_argument("--number", default=None)
    ap.add_argument("--seller", default="Testivarustamo Oy")
    ap.add_argument("--business-id", default="1234567-8")
    ap.add_argument("--vat-id", default="FI12345678")
    ap.add_argument("--iban", default="FI2112345600000785")
    ap.add_argument("--bic", default="NDEAFIHH")
    ap.add_argument("--currency", default="EUR")
    ap.add_argument("--container", default=None)
    ap.add_argument("--line", action="append", default=[], help="description:amount:vat_pct (net amount)")
    a = ap.parse_args()
    rows = []
    for spec in a.line or ["Merirahti / Ocean freight:1900.00:0", "Jakelu / Delivery:300.00:25.5"]:
        name, amount, rate = spec.rsplit(":", 2)
        rows.append({"name": name, "amount_cents": int((Decimal(amount) * 100).to_integral_value()), "vat_rate_pct": float(rate),
                     "free_text": [" ".join(x for x in [a.shipment_ref, a.container] if x)]})
    number = a.number or f"{a.shipment_ref[-6:]}01"
    inv = invoice_dict(number=number, rows=rows, currency=a.currency,
                       seller={"name": a.seller, "business_id": a.business_id, "vat_id": a.vat_id, "iban": a.iban,
                               "bic": a.bic, "country": a.vat_id[:2]})
    os.makedirs(a.out, exist_ok=True)
    xml_path = os.path.join(a.out, f"finvoice-{number}.xml")
    pdf_path = os.path.join(a.out, f"invoice-{number}.pdf")
    with open(xml_path, "wb") as fh:
        fh.write(make_finvoice(inv))
    with open(pdf_path, "wb") as fh:
        fh.write(make_pdf(inv))
    print(xml_path)
    print(pdf_path)


if __name__ == "__main__":
    main()
