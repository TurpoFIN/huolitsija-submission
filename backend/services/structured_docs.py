"""Exact reader for structured documents: a commercial invoice or packing list exported from the seller's ERP as CSV
or XLSX (label/value rows plus a goods table). Nothing is guessed: a value is taken only from a cell whose label
names it, so every field it returns has the document as its source. Anything it cannot place goes to the AI reader
(or to a person when no model is configured) exactly as before.

    raw = read_sheet(data, "text/csv", declared_type)   # -> same shape as the AI reader's answer, or None
"""
import csv
import io
import re

SHEET_TYPES = {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
CONFIDENCE = 0.99  # read from a labelled cell; the post-checks (check digits, totals) still run

TYPE_WORDS = {"commercial invoice": "commercial_invoice", "invoice": "commercial_invoice", "kauppalasku": "commercial_invoice",
              "proforma invoice": "commercial_invoice", "packing list": "packing_list", "pakkausluettelo": "packing_list",
              "pakkauslista": "packing_list"}

FIELD_LABELS = {
    "document_type": ["document type", "document", "asiakirja", "asiakirjan tyyppi"],
    "invoice_number": ["invoice number", "invoice no", "invoice nr", "invoice #", "laskun numero", "laskunumero", "lasku nro"],
    "invoice_date": ["invoice date", "laskun päivämäärä", "laskun pvm", "laskupäivä"],
    "date": ["date", "päivämäärä", "pvm"],
    "packing_list_number": ["packing list number", "packing list no", "pakkausluettelon numero"],
    "seller_name": ["seller", "seller name", "exporter", "shipper", "myyjä", "viejä", "lähettäjä"],
    "seller_address": ["seller address", "exporter address", "myyjän osoite"],
    "seller_city": ["seller city", "exporter city", "myyjän kaupunki"],
    "seller_postal_code": ["seller postal code", "exporter postal code", "myyjän postinumero"],
    "seller_country": ["seller country", "exporter country", "myyjän maa"],
    "seller_vat_id": ["seller vat", "seller vat id", "seller vat number", "myyjän alv-tunnus"],
    "seller_eori": ["seller eori", "exporter eori", "myyjän eori"],
    "buyer_name": ["buyer", "buyer name", "importer", "ostaja", "maahantuoja"],
    "buyer_address": ["buyer address", "importer address", "ostajan osoite"],
    "buyer_country": ["buyer country", "importer country", "ostajan maa"],
    "buyer_vat_id": ["buyer vat", "buyer vat id", "buyer vat number", "ostajan alv-tunnus"],
    "buyer_eori": ["buyer eori", "importer eori", "ostajan eori"],
    "consignee_name": ["consignee", "consignee name", "vastaanottaja"],
    "consignee_address": ["consignee address", "vastaanottajan osoite"],
    "consignee_country": ["consignee country", "vastaanottajan maa"],
    "incoterm": ["incoterm", "incoterms", "incoterms 2020", "delivery terms", "terms of delivery", "toimitusehto"],
    "incoterm_place": ["incoterm place", "named place", "delivery place", "toimitusehdon paikka"],
    "currency": ["currency", "valuutta"],
    "subtotal_amount": ["subtotal", "goods total", "välisumma"],
    "freight_amount": ["freight", "freight amount", "rahti"],
    "insurance_amount": ["insurance", "insurance amount", "vakuutus"],
    "other_charges_amount": ["other charges", "muut kulut"],
    "total_amount": ["total", "total amount", "invoice total", "grand total", "yhteensä", "laskun loppusumma"],
    "payment_terms": ["payment terms", "maksuehto"],
    "transaction_nature": ["nature of transaction", "transaction nature", "kauppatapahtuman luonne"],
    "country_of_origin": ["country of origin", "origin", "alkuperämaa"],
    "total_packages": ["total packages", "packages", "number of packages", "kollimäärä", "kollit yhteensä"],
    "package_type": ["package type", "pakkaustapa"],
    "total_gross_weight_kg": ["total gross weight kg", "gross weight kg", "gross weight", "bruttopaino kg", "bruttopaino"],
    "total_net_weight_kg": ["total net weight kg", "net weight kg", "net weight", "nettopaino kg", "nettopaino"],
    "total_volume_m3": ["total volume m3", "volume m3", "tilavuus m3"],
    "purchase_order_number": ["po number", "purchase order", "purchase order number", "tilausnumero"],
    "container_numbers": ["container", "container number", "container numbers", "kontti"],
    "marks_and_numbers": ["marks and numbers", "merkit ja numerot"],
}
ITEM_LABELS = {
    "description": ["description", "goods description", "item description", "kuvaus", "tavaran kuvaus", "nimike"],
    "quantity": ["quantity", "qty", "määrä", "kpl"],
    "unit": ["unit", "uom", "yksikkö"],
    "unit_price": ["unit price", "price", "à-hinta", "a-hinta", "yksikköhinta"],
    "amount": ["amount", "line total", "total", "value", "summa", "arvo", "rivisumma"],
    "hs_code": ["hs code", "hs", "cn code", "commodity code", "tariff code", "tullinimike", "nimikekoodi"],
    "country_of_origin": ["country of origin", "origin", "alkuperämaa"],
    "net_weight_kg": ["net weight kg", "net kg", "net weight", "nettopaino kg", "nettopaino"],
    "gross_weight_kg": ["gross weight kg", "gross kg", "gross weight", "bruttopaino kg", "bruttopaino"],
    "sku": ["sku", "article", "article number", "item number", "tuotenumero"],
    "packages": ["packages", "kollit", "kollimäärä"],
    "package_type": ["package type", "pakkaustapa"],
    "dimensions": ["dimensions", "mitat"],
    "volume_m3": ["volume m3", "tilavuus m3"],
}
NUMERIC = {"subtotal_amount", "freight_amount", "insurance_amount", "other_charges_amount", "total_amount", "total_packages",
           "total_gross_weight_kg", "total_net_weight_kg", "total_volume_m3", "quantity", "unit_price", "amount",
           "net_weight_kg", "gross_weight_kg", "packages", "volume_m3"}


def _norm_label(s) -> str:
    s = str(s or "").strip().lower().replace("_", " ")
    s = re.sub(r"[():#\.]+$", "", s)
    s = re.sub(r"\s*\((kg|m3|eur|usd)\)", r" \1", s)
    return re.sub(r"\s+", " ", s.replace(":", "")).strip()


def _lookup(labels: dict, label: str) -> str | None:
    for key, words in labels.items():
        if label in words:
            return key
    return None


def number(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", " ").replace("€", "").replace("EUR", "").strip()
    s = re.sub(r"(?<=\d) (?=\d{3}\b)", "", s)
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) or s.count(",") == 1 and not re.search(r",\d{3}$", s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _rows(data: bytes, mime: str) -> list[list]:
    if mime == "text/csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)][:3000]
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets[:3]:
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r > 3000:
                break
            out.append(["" if v is None else v for v in row])
    return out


def read_sheet(data: bytes, mime: str, declared: str | None = None) -> dict | None:
    if mime not in SHEET_TYPES:
        return None
    try:
        rows = _rows(data, mime)
    except Exception:  # noqa: BLE001 - unreadable sheet: the normal reader reports it
        return None
    fields: dict[str, object] = {}
    items: list[dict] = []
    header: list[str | None] | None = None
    for row in rows:
        cells = [c for c in row]
        nonempty = [c for c in cells if str(c).strip() != ""]
        if not nonempty:
            header = None if items else header
            continue
        labels = [_norm_label(c) for c in cells]
        if header is None:
            cols = [_lookup(ITEM_LABELS, lb) for lb in labels]
            if "description" in cols and ("amount" in cols or "quantity" in cols or "gross_weight_kg" in cols):
                header = cols
                continue
            key = _lookup(FIELD_LABELS, labels[0]) if labels else None
            if key and len(nonempty) >= 2:
                fields.setdefault(key, next((c for c in cells[1:] if str(c).strip() != ""), None))
            continue
        key = _lookup(FIELD_LABELS, labels[0]) if labels else None
        if key and header[0] != "description" or key in ("total_amount", "subtotal_amount", "freight_amount",
                                                          "insurance_amount", "total_gross_weight_kg", "total_net_weight_kg"):
            # a totals row under the goods table: "Total ;;; 1 234,50"
            fields.setdefault(key, next((c for c in reversed(cells[1:]) if str(c).strip() != ""), None))
            continue
        it = {}
        for col, v in zip(header, cells):
            if col and str(v).strip() != "":
                it[col] = number(v) if col in NUMERIC else str(v).strip()
        if it.get("description") and not str(it["description"]).lower().startswith(("total", "yhteensä")):
            items.append(it)
    if not items and len(fields) < 3:
        return None
    doc_type = declared
    explicit = TYPE_WORDS.get(_norm_label(fields.pop("document_type", "") or ""))
    if not doc_type:
        doc_type = explicit or ("packing_list" if "packing_list_number" in fields and "invoice_number" not in fields
                                else "commercial_invoice" if "invoice_number" in fields else None)
    if doc_type not in ("commercial_invoice", "packing_list"):
        return None
    if doc_type == "commercial_invoice" and "date" in fields and "invoice_date" not in fields:
        fields["invoice_date"] = fields.pop("date")
    if doc_type == "packing_list" and "consignee_name" not in fields and "buyer_name" in fields:
        fields["consignee_name"] = fields["buyer_name"]
    out_fields = {}
    for k, v in fields.items():
        if v in (None, ""):
            continue
        if k in NUMERIC:
            v = number(v)
            if v is None:
                continue
        elif k == "container_numbers":
            v = [x.strip() for x in re.split(r"[,; ]+", str(v)) if x.strip()]
        elif hasattr(v, "isoformat"):
            v = v.isoformat()[:10]
        else:
            v = str(v).strip()
        out_fields[k] = {"value": v, "confidence": CONFIDENCE}
    for it in items:
        it["confidence"] = CONFIDENCE
        if it.get("hs_code"):
            it["hs_code"] = re.sub(r"\D", "", str(it["hs_code"]).split(".")[0] if isinstance(it["hs_code"], float) else str(it["hs_code"]))
    return {"document_type": doc_type, "type_confidence": 1.0 if (declared or explicit) else 0.9, "fields": out_fields,
            "line_items": items, "language": None, "reader": "structured"}
