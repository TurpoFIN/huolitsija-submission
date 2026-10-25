"""Structured e-invoices from carriers: Finvoice 3.0 (Finanssiala, Finnish bank e-invoice standard) and PEPPOL BIS
Billing 3.0 (OASIS UBL 2.1 Invoice / CreditNote). Parsing is deterministic: no model is involved and every field comes
from a named element.

Finvoice 3.0 messages are validated against the official schema published by Finanssiala:
  https://file.finanssiala.fi/finvoice/Finvoice3.0.xsd  (stored as backend/data/finvoice/Finvoice3.0.xsd, see SOURCE.txt)
Implementation guidelines: https://file.finanssiala.fi/finvoice/Finvoice_3_0_implementation_guidelines.pdf
PEPPOL BIS Billing 3.0 is parsed by element names (cbc/cac); its UBL schema and Schematron are not run here.

    from services.finvoice import detect, parse, validate_finvoice, build_finvoice
    kind = detect(xml_bytes)            # "finvoice" | "ubl" | None
    fields = parse(xml_bytes)           # normalized carrier-invoice fields (see _empty())
"""
from __future__ import annotations

import os
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from xml.sax.saxutils import escape

from lxml import etree

XSD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "finvoice", "Finvoice3.0.xsd")
XSD_URL = "https://file.finanssiala.fi/finvoice/Finvoice3.0.xsd"
UBL_INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NS = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
# Finvoice InvoiceTypeCode: INV01 invoice, INV02 credit note (hyvityslasku); a few other INV codes exist.
CREDIT_TYPE_CODES = {"INV02"}
UBL_CREDIT_TYPE_CODES = {"381", "396", "532"}

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_blank_text=False)


class InvoiceParseError(Exception):
    pass


def _root(data: bytes):
    try:
        return etree.fromstring(data, _PARSER)
    except etree.XMLSyntaxError as exc:
        raise InvoiceParseError(f"Not well-formed XML: {exc}") from None


def detect(data: bytes) -> str | None:
    head = data[:4096].lstrip()
    if not head.startswith(b"<"):
        return None
    try:
        root = _root(data)
    except InvoiceParseError:
        return None
    tag = etree.QName(root).localname
    ns = etree.QName(root).namespace
    if tag == "Finvoice":
        return "finvoice"
    if (tag, ns) in (("Invoice", UBL_INVOICE_NS), ("CreditNote", UBL_CREDIT_NS)):
        return "ubl"
    # SOAP-wrapped Finvoice (bank channel): the Finvoice element follows the envelope.
    if root.find(".//Finvoice") is not None:
        return "finvoice"
    return None


@lru_cache(maxsize=1)
def _schema():
    return etree.XMLSchema(etree.parse(XSD_PATH))


def validate_finvoice(data: bytes) -> dict:
    """{valid, errors[], schema, schema_url} against the official Finvoice 3.0 XSD."""
    root = _root(data)
    fin = root if etree.QName(root).localname == "Finvoice" else root.find(".//Finvoice")
    schema = _schema()
    ok = schema.validate(etree.ElementTree(fin))
    errors = [f"line {e.line}: {e.message}" for e in schema.error_log][:20]
    return {"valid": bool(ok), "errors": errors, "schema": "Finvoice3.0.xsd", "schema_url": XSD_URL}


# --------------------------------------------------------------------------------------------- helpers
def cents(value) -> int | None:
    """'1 234,50' / '1234.50' / '-12,5' -> integer cents (half-up). None when empty or not a number."""
    if value is None:
        return None
    s = str(value).strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def pct(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def fdate(value) -> str | None:
    """Finvoice CCYYMMDD or ISO date -> YYYY-MM-DD."""
    s = (value or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s[:10]):
        return s[:10]
    return None


def _t(el, path: str) -> str | None:
    if el is None:
        return None
    x = el.find(path)
    if x is None or x.text is None:
        return None
    return x.text.strip() or None


def _all(el, path: str) -> list[str]:
    return [x.text.strip() for x in el.findall(path) if x.text and x.text.strip()] if el is not None else []


def _attr(el, path: str, name: str) -> str | None:
    x = el.find(path) if el is not None else None
    return x.get(name) if x is not None else None


def _empty(fmt: str) -> dict:
    return {"format": fmt, "document_type": "invoice", "type_code": None,
            "supplier": {"name": None, "business_id": None, "vat_id": None, "country": None, "iban": None, "bic": None,
                         "address": None},
            "buyer": {"name": None, "business_id": None, "vat_id": None},
            "number": None, "invoice_date": None, "due_date": None, "currency": None, "net_cents": None,
            "vat_cents": None, "total_cents": None, "lines": [], "vat_breakdown": [],
            "payment": {"iban": None, "bic": None, "reference": None, "message": None},
            "original_invoice_number": None, "order_ref": None, "buyer_reference": None, "free_text": [], "text": ""}


def _country_of(vat_id: str | None, fallback: str | None) -> str | None:
    if fallback:
        return fallback.upper()[:2]
    if vat_id and re.match(r"[A-Z]{2}", vat_id.strip().upper()):
        cc = vat_id.strip().upper()[:2]
        return "GR" if cc == "EL" else cc
    return None


def _norm_iban(v: str | None) -> str | None:
    return re.sub(r"\s", "", v).upper() if v else None


# --------------------------------------------------------------------------------------------- Finvoice 3.0
def parse_finvoice(data: bytes) -> dict:
    root = _root(data)
    fin = root if etree.QName(root).localname == "Finvoice" else root.find(".//Finvoice")
    if fin is None:
        raise InvoiceParseError("No Finvoice element in the message")
    out = _empty("finvoice-3.0")
    out["version"] = fin.get("Version")
    sp = fin.find("SellerPartyDetails")
    sup = out["supplier"]
    sup["name"] = " ".join(_all(sp, "SellerOrganisationName")) or None
    sup["business_id"] = _t(sp, "SellerPartyIdentifier")
    sup["vat_id"] = (_t(sp, "SellerOrganisationTaxCode") or "").replace(" ", "").upper() or None
    addr = sp.find("SellerPostalAddressDetails") if sp is not None else None
    if addr is not None:
        sup["address"] = {"street": " ".join(_all(addr, "SellerStreetName")) or None, "city": _t(addr, "SellerTownName"),
                          "postal_code": _t(addr, "SellerPostCodeIdentifier"), "country": _t(addr, "CountryCode")}
    sup["country"] = _country_of(sup["vat_id"], (sup.get("address") or {}).get("country"))
    acc = fin.find("SellerInformationDetails/SellerAccountDetails")
    sup["iban"] = _norm_iban(_t(acc, "SellerAccountID"))
    sup["bic"] = (_t(acc, "SellerBic") or "").upper() or None
    bp = fin.find("BuyerPartyDetails")
    out["buyer"] = {"name": " ".join(_all(bp, "BuyerOrganisationName")) or None,
                    "business_id": _t(bp, "BuyerPartyIdentifier"),
                    "vat_id": (_t(bp, "BuyerOrganisationTaxCode") or "").replace(" ", "").upper() or None}
    det = fin.find("InvoiceDetails")
    if det is None:
        raise InvoiceParseError("Finvoice has no InvoiceDetails")
    out["type_code"] = _t(det, "InvoiceTypeCode")
    out["type_text"] = _t(det, "InvoiceTypeText")
    out["number"] = _t(det, "InvoiceNumber")
    out["invoice_date"] = fdate(_t(det, "InvoiceDate"))
    out["original_invoice_number"] = _t(det, "OriginalInvoiceNumber")
    out["order_ref"] = _t(det, "OrderIdentifier")
    out["buyer_reference"] = _t(det, "BuyerReferenceIdentifier")
    out["free_text"] = _all(det, "InvoiceFreeText")
    out["currency"] = (_attr(det, "InvoiceTotalVatIncludedAmount", "AmountCurrencyIdentifier") or "").upper() or None
    out["net_cents"] = cents(_t(det, "InvoiceTotalVatExcludedAmount"))
    out["vat_cents"] = cents(_t(det, "InvoiceTotalVatAmount"))
    out["total_cents"] = cents(_t(det, "InvoiceTotalVatIncludedAmount"))
    out["due_date"] = fdate(_t(det, "PaymentTermsDetails/InvoiceDueDate"))
    for vs in det.findall("VatSpecificationDetails"):
        out["vat_breakdown"].append({"base_cents": cents(_t(vs, "VatBaseAmount")), "rate_pct": pct(_t(vs, "VatRatePercent")),
                                     "vat_cents": cents(_t(vs, "VatRateAmount")), "code": _t(vs, "VatCode"),
                                     "text": " ".join(_all(vs, "VatFreeText")) or None})
    for row in fin.findall("InvoiceRow"):
        free = _all(row, "RowFreeText")
        name = _t(row, "ArticleName")
        desc = " ".join(x for x in [name, _t(row, "ArticleDescription")] if x) or (free[0] if free else None)
        net = cents(_t(row, "RowVatExcludedAmount"))
        vat = cents(_t(row, "RowVatAmount"))
        gross = cents(_t(row, "RowAmount"))
        if net is None and gross is not None and vat is not None:
            net = gross - vat
        out["lines"].append({
            "description": desc, "article": _t(row, "ArticleIdentifier"), "quantity": pct(_t(row, "InvoicedQuantity")),
            "unit_price_cents": cents(_t(row, "UnitPriceAmount")), "amount_cents": net, "vat_rate_pct": pct(_t(row, "RowVatRatePercent")),
            "vat_cents": vat, "gross_cents": gross, "vat_code": _t(row, "RowVatCode"), "free_text": free,
            "row_refs": [x for x in [_t(row, "RowIdentifier"), _t(row, "RowDeliveryIdentifier"),
                                     _t(row, "RowBuyerReferenceIdentifier"), _t(row, "RowAgreementIdentifier")] if x]})
    epi = fin.find("EpiDetails")
    pay = out["payment"]
    pay["iban"] = _norm_iban(_t(epi, "EpiPartyDetails/EpiBeneficiaryPartyDetails/EpiAccountID")) or sup["iban"]
    pay["bic"] = (_t(epi, "EpiPartyDetails/EpiBfiPartyDetails/EpiBfiIdentifier") or "").upper() or sup["bic"]
    pay["reference"] = _t(epi, "EpiPaymentInstructionDetails/EpiRemittanceInfoIdentifier")
    pay["reference_scheme"] = _attr(epi, "EpiPaymentInstructionDetails/EpiRemittanceInfoIdentifier", "IdentificationSchemeName")
    pay["instructed_cents"] = cents(_t(epi, "EpiPaymentInstructionDetails/EpiInstructedAmount"))
    pay["message"] = " ".join(out["free_text"]) or None
    out["due_date"] = out["due_date"] or fdate(_t(epi, "EpiPaymentInstructionDetails/EpiDateOptionDate"))
    if not sup["iban"]:
        sup["iban"], sup["bic"] = pay["iban"], pay["bic"]
    if (out["type_code"] or "").upper() in CREDIT_TYPE_CODES or (out["total_cents"] or 0) < 0:
        out["document_type"] = "credit_note"
    out["text"] = " ".join(t.strip() for t in fin.itertext() if t and t.strip())
    return out


# --------------------------------------------------------------------------------------------- PEPPOL BIS 3 (UBL)
def _u(el, path: str) -> str | None:
    return _t(el, path.replace("cac:", f"{{{CAC}}}").replace("cbc:", f"{{{CBC}}}"))


def _ue(el, path: str):
    return el.find(path.replace("cac:", f"{{{CAC}}}").replace("cbc:", f"{{{CBC}}}")) if el is not None else None


def _uall(el, path: str):
    return el.findall(path.replace("cac:", f"{{{CAC}}}").replace("cbc:", f"{{{CBC}}}")) if el is not None else []


def parse_ubl(data: bytes) -> dict:
    root = _root(data)
    credit = etree.QName(root).localname == "CreditNote"
    out = _empty("peppol-bis-3")
    out["number"] = _u(root, "cbc:ID")
    out["invoice_date"] = fdate(_u(root, "cbc:IssueDate"))
    out["due_date"] = fdate(_u(root, "cbc:DueDate") or _u(root, "cac:PaymentMeans/cbc:PaymentDueDate")
                            or _u(root, "cac:PaymentTerms/cbc:PaymentDueDate"))
    out["type_code"] = _u(root, "cbc:CreditNoteTypeCode" if credit else "cbc:InvoiceTypeCode")
    out["currency"] = (_u(root, "cbc:DocumentCurrencyCode") or "").upper() or None
    out["buyer_reference"] = _u(root, "cbc:BuyerReference")
    out["order_ref"] = _u(root, "cac:OrderReference/cbc:ID")
    out["original_invoice_number"] = _u(root, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID")
    out["free_text"] = [x.text.strip() for x in _uall(root, "cbc:Note") if x.text and x.text.strip()]
    party = _ue(root, "cac:AccountingSupplierParty/cac:Party")
    sup = out["supplier"]
    sup["name"] = _u(party, "cac:PartyLegalEntity/cbc:RegistrationName") or _u(party, "cac:PartyName/cbc:Name")
    sup["vat_id"] = (_u(party, "cac:PartyTaxScheme/cbc:CompanyID") or "").replace(" ", "").upper() or None
    sup["business_id"] = _u(party, "cac:PartyLegalEntity/cbc:CompanyID")
    cc = _u(party, "cac:PostalAddress/cac:Country/cbc:IdentificationCode")
    sup["address"] = {"street": _u(party, "cac:PostalAddress/cbc:StreetName"), "city": _u(party, "cac:PostalAddress/cbc:CityName"),
                      "postal_code": _u(party, "cac:PostalAddress/cbc:PostalZone"), "country": cc}
    sup["country"] = _country_of(sup["vat_id"], cc)
    bparty = _ue(root, "cac:AccountingCustomerParty/cac:Party")
    out["buyer"] = {"name": _u(bparty, "cac:PartyLegalEntity/cbc:RegistrationName") or _u(bparty, "cac:PartyName/cbc:Name"),
                    "business_id": _u(bparty, "cac:PartyLegalEntity/cbc:CompanyID"),
                    "vat_id": (_u(bparty, "cac:PartyTaxScheme/cbc:CompanyID") or "").upper() or None}
    pm = _ue(root, "cac:PaymentMeans")
    pay = out["payment"]
    pay["iban"] = _norm_iban(_u(pm, "cac:PayeeFinancialAccount/cbc:ID"))
    pay["bic"] = (_u(pm, "cac:PayeeFinancialAccount/cac:FinancialInstitutionBranch/cbc:ID") or "").upper() or None
    pay["reference"] = _u(pm, "cbc:PaymentID")
    pay["message"] = " ".join(out["free_text"]) or None
    sup["iban"], sup["bic"] = pay["iban"], pay["bic"]
    lmt = _ue(root, "cac:LegalMonetaryTotal")
    out["net_cents"] = cents(_u(lmt, "cbc:TaxExclusiveAmount"))
    out["total_cents"] = cents(_u(lmt, "cbc:TaxInclusiveAmount"))
    payable = cents(_u(lmt, "cbc:PayableAmount"))
    vat_total = None
    for tt in _uall(root, "cac:TaxTotal"):
        amt = _ue(tt, "cbc:TaxAmount")
        if amt is not None and (amt.get("currencyID") or out["currency"]) == out["currency"]:
            vat_total = cents(amt.text)
        for sub in _uall(tt, "cac:TaxSubtotal"):
            out["vat_breakdown"].append({"base_cents": cents(_u(sub, "cbc:TaxableAmount")), "vat_cents": cents(_u(sub, "cbc:TaxAmount")),
                                         "rate_pct": pct(_u(sub, "cac:TaxCategory/cbc:Percent")),
                                         "code": _u(sub, "cac:TaxCategory/cbc:ID"),
                                         "text": _u(sub, "cac:TaxCategory/cbc:TaxExemptionReason")})
    out["vat_cents"] = vat_total
    if out["total_cents"] is None:
        out["total_cents"] = payable
    for ln in _uall(root, "cac:CreditNoteLine" if credit else "cac:InvoiceLine"):
        notes = [x.text.strip() for x in _uall(ln, "cbc:Note") if x.text and x.text.strip()]
        name = _u(ln, "cac:Item/cbc:Name")
        desc = " ".join(x for x in [name, _u(ln, "cac:Item/cbc:Description")] if x) or (notes[0] if notes else None)
        rate = pct(_u(ln, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent"))
        net = cents(_u(ln, "cbc:LineExtensionAmount"))
        out["lines"].append({"description": desc, "article": _u(ln, "cac:Item/cac:SellersItemIdentification/cbc:ID"),
                             "quantity": pct(_u(ln, "cbc:CreditedQuantity" if credit else "cbc:InvoicedQuantity")),
                             "unit_price_cents": cents(_u(ln, "cac:Price/cbc:PriceAmount")), "amount_cents": net,
                             "vat_rate_pct": rate,
                             "vat_cents": int((Decimal(net) * Decimal(str(rate)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                             if net is not None and rate is not None else None,
                             "vat_code": _u(ln, "cac:Item/cac:ClassifiedTaxCategory/cbc:ID"), "free_text": notes,
                             "row_refs": [x for x in [_u(ln, "cac:DocumentReference/cbc:ID"), _u(ln, "cac:OrderLineReference/cbc:LineID")] if x]})
    if credit:
        out["document_type"] = "credit_note"
        # Credit notes carry positive amounts in UBL; store them negative so they net against the invoice.
        for k in ("net_cents", "vat_cents", "total_cents"):
            if out[k] is not None:
                out[k] = -abs(out[k])
        for ln in out["lines"]:
            for k in ("amount_cents", "vat_cents"):
                if ln.get(k) is not None:
                    ln[k] = -abs(ln[k])
    elif (out["type_code"] or "") in UBL_CREDIT_TYPE_CODES or (out["total_cents"] or 0) < 0:
        out["document_type"] = "credit_note"
    out["text"] = " ".join(t.strip() for t in root.itertext() if t and t.strip())
    return out


def parse(data: bytes) -> dict:
    kind = detect(data)
    if kind == "finvoice":
        return parse_finvoice(data)
    if kind == "ubl":
        return parse_ubl(data)
    raise InvoiceParseError("The XML is neither a Finvoice 3.0 message nor a PEPPOL BIS Billing 3.0 invoice")


# --------------------------------------------------------------------------------------------- builder
def _amt(c: int) -> str:
    sign = "-" if c < 0 else ""
    c = abs(int(c))
    return f"{sign}{c // 100},{c % 100:02d}"


def _d(iso: str) -> str:
    return iso.replace("-", "")[:8]


def _pct(v: float) -> str:
    s = f"{Decimal(str(v)).normalize():f}".replace(".", ",")
    return s


def build_finvoice(inv: dict) -> bytes:
    """Finvoice 3.0 message from a plain dict (used by the fixture generator and tests).

    inv = {number, invoice_date, due_date, currency, type_code "INV01"|"INV02", original_invoice_number?,
           seller{name, business_id, vat_id, street, postal_code, city, country, iban, bic},
           buyer{name, business_id, vat_id, street, postal_code, city, country},
           reference, rows[{name, description?, free_text[], quantity, unit_price_cents, amount_cents (net),
           vat_rate_pct, vat_cents}], free_text[]}"""
    cur = inv.get("currency") or "EUR"
    s, b = inv["seller"], inv["buyer"]
    rows = inv["rows"]
    net = sum(r["amount_cents"] for r in rows)
    vat = sum(r["vat_cents"] for r in rows)
    total = net + vat
    e = escape
    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<Finvoice Version="3.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
         'xsi:noNamespaceSchemaLocation="Finvoice3.0.xsd">']
    x.append("<SellerPartyDetails>")
    if s.get("business_id"):
        x.append(f"<SellerPartyIdentifier>{e(s['business_id'])}</SellerPartyIdentifier>")
    x.append(f"<SellerOrganisationName>{e(s['name'])}</SellerOrganisationName>")
    if s.get("vat_id"):
        x.append(f"<SellerOrganisationTaxCode>{e(s['vat_id'])}</SellerOrganisationTaxCode>")
    x.append(f"<SellerPostalAddressDetails><SellerStreetName>{e(s.get('street') or 'Satamakatu 1')}</SellerStreetName>"
             f"<SellerTownName>{e(s.get('city') or 'Helsinki')}</SellerTownName>"
             f"<SellerPostCodeIdentifier>{e(s.get('postal_code') or '00100')}</SellerPostCodeIdentifier>"
             f"<CountryCode>{e(s.get('country') or 'FI')}</CountryCode></SellerPostalAddressDetails>")
    x.append("</SellerPartyDetails>")
    x.append("<SellerInformationDetails>")
    x.append(f"<SellerAccountDetails><SellerAccountID IdentificationSchemeName=\"IBAN\">{e(s['iban'])}</SellerAccountID>"
             f"<SellerBic IdentificationSchemeName=\"BIC\">{e(s['bic'])}</SellerBic></SellerAccountDetails>")
    x.append("</SellerInformationDetails>")
    x.append("<BuyerPartyDetails>")
    if b.get("business_id"):
        x.append(f"<BuyerPartyIdentifier>{e(b['business_id'])}</BuyerPartyIdentifier>")
    x.append(f"<BuyerOrganisationName>{e(b['name'])}</BuyerOrganisationName>")
    if b.get("vat_id"):
        x.append(f"<BuyerOrganisationTaxCode>{e(b['vat_id'])}</BuyerOrganisationTaxCode>")
    x.append(f"<BuyerPostalAddressDetails><BuyerStreetName>{e(b.get('street') or 'Testikatu 1')}</BuyerStreetName>"
             f"<BuyerTownName>{e(b.get('city') or 'Helsinki')}</BuyerTownName>"
             f"<BuyerPostCodeIdentifier>{e(b.get('postal_code') or '00100')}</BuyerPostCodeIdentifier>"
             f"<CountryCode>{e(b.get('country') or 'FI')}</CountryCode></BuyerPostalAddressDetails>")
    x.append("</BuyerPartyDetails>")
    code = inv.get("type_code") or "INV01"
    x.append("<InvoiceDetails>")
    x.append(f"<InvoiceTypeCode>{code}</InvoiceTypeCode>")
    x.append(f"<InvoiceTypeText>{'HYVITYSLASKU' if code in CREDIT_TYPE_CODES else 'LASKU'}</InvoiceTypeText>")
    x.append("<OriginCode>Original</OriginCode>")
    x.append(f"<InvoiceNumber>{e(inv['number'])}</InvoiceNumber>")
    x.append(f"<InvoiceDate Format=\"CCYYMMDD\">{_d(inv['invoice_date'])}</InvoiceDate>")
    if inv.get("original_invoice_number"):
        x.append(f"<OriginalInvoiceNumber>{e(inv['original_invoice_number'])}</OriginalInvoiceNumber>")
    if inv.get("order_ref"):
        x.append(f"<OrderIdentifier>{e(inv['order_ref'])}</OrderIdentifier>")
    x.append(f"<InvoiceTotalVatExcludedAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(net)}</InvoiceTotalVatExcludedAmount>")
    x.append(f"<InvoiceTotalVatAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(vat)}</InvoiceTotalVatAmount>")
    x.append(f"<InvoiceTotalVatIncludedAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(total)}</InvoiceTotalVatIncludedAmount>")
    rates: dict[float, list[int]] = {}
    for r in rows:
        rates.setdefault(float(r["vat_rate_pct"]), [0, 0])
        rates[float(r["vat_rate_pct"])][0] += r["amount_cents"]
        rates[float(r["vat_rate_pct"])][1] += r["vat_cents"]
    for rate, (base, v) in sorted(rates.items()):
        x.append(f"<VatSpecificationDetails><VatBaseAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(base)}</VatBaseAmount>"
                 f"<VatRatePercent>{_pct(rate)}</VatRatePercent>"
                 f"<VatRateAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(v)}</VatRateAmount>"
                 + (f"<VatFreeText>{e(inv.get('vat_free_text') or 'AVL 71 § kansainvälinen kuljetus')}</VatFreeText>" if rate == 0 else "")
                 + "</VatSpecificationDetails>")
    for t in inv.get("free_text") or []:
        x.append(f"<InvoiceFreeText>{e(t)}</InvoiceFreeText>")
    x.append(f"<PaymentTermsDetails><PaymentTermsFreeText>{e(inv.get('terms_text') or '14 pv netto')}</PaymentTermsFreeText>"
             f"<InvoiceDueDate Format=\"CCYYMMDD\">{_d(inv['due_date'])}</InvoiceDueDate></PaymentTermsDetails>")
    x.append("</InvoiceDetails>")
    for r in rows:
        x.append("<InvoiceRow>")
        if r.get("article"):
            x.append(f"<ArticleIdentifier>{e(r['article'])}</ArticleIdentifier>")
        x.append(f"<ArticleName>{e(r['name'])}</ArticleName>")
        q = r.get("quantity") or 1
        x.append(f"<InvoicedQuantity QuantityUnitCode=\"kpl\">{_pct(q)}</InvoicedQuantity>")
        unit = r.get("unit_price_cents", r["amount_cents"])
        x.append(f"<UnitPriceAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(unit)}</UnitPriceAmount>")
        if r.get("row_ref"):
            x.append(f"<RowIdentifier>{e(r['row_ref'])}</RowIdentifier>")
        if r.get("delivery_ref"):
            x.append(f"<RowDeliveryIdentifier>{e(r['delivery_ref'])}</RowDeliveryIdentifier>")
        for t in r.get("free_text") or []:
            x.append(f"<RowFreeText>{e(t)}</RowFreeText>")
        x.append(f"<RowVatRatePercent>{_pct(r['vat_rate_pct'])}</RowVatRatePercent>")
        x.append(f"<RowVatAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(r['vat_cents'])}</RowVatAmount>")
        x.append(f"<RowVatExcludedAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(r['amount_cents'])}</RowVatExcludedAmount>")
        x.append(f"<RowAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(r['amount_cents'] + r['vat_cents'])}</RowAmount>")
        x.append("</InvoiceRow>")
    x.append("<EpiDetails><EpiIdentificationDetails>"
             f"<EpiDate Format=\"CCYYMMDD\">{_d(inv['invoice_date'])}</EpiDate><EpiReference>{e(inv['number'])}</EpiReference>"
             "</EpiIdentificationDetails><EpiPartyDetails><EpiBfiPartyDetails>"
             f"<EpiBfiIdentifier IdentificationSchemeName=\"BIC\">{e(s['bic'])}</EpiBfiIdentifier></EpiBfiPartyDetails>"
             f"<EpiBeneficiaryPartyDetails><EpiNameAddressDetails>{e(s['name'][:35])}</EpiNameAddressDetails>"
             + (f"<EpiBei>{e(s['vat_id'])}</EpiBei>" if s.get("vat_id") and 8 <= len(s["vat_id"]) <= 11 else "")
             + f"<EpiAccountID IdentificationSchemeName=\"IBAN\">{e(s['iban'])}</EpiAccountID></EpiBeneficiaryPartyDetails>"
             "</EpiPartyDetails><EpiPaymentInstructionDetails>"
             + (f"<EpiRemittanceInfoIdentifier IdentificationSchemeName=\"{'ISO' if str(inv['reference']).startswith('RF') else 'SPY'}\">"
                f"{e(str(inv['reference']).replace(' ', ''))}</EpiRemittanceInfoIdentifier>" if inv.get("reference") else "")
             + f"<EpiInstructedAmount AmountCurrencyIdentifier=\"{cur}\">{_amt(total)}</EpiInstructedAmount>"
             "<EpiCharge ChargeOption=\"SHA\">SHA</EpiCharge>"
             f"<EpiDateOptionDate Format=\"CCYYMMDD\">{_d(inv['due_date'])}</EpiDateOptionDate>"
             "</EpiPaymentInstructionDetails></EpiDetails>")
    x.append("</Finvoice>")
    return "\n".join(x).encode("utf-8")
