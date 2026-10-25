"""ISO 20022 payment messages for supplier payments: pain.001 (credit transfer initiation, we generate), pain.002
(payment status report, the bank sends) and camt.054 (debit/credit notification, the bank sends).

Versions (official schemas in backend/data/sepa/, see its README for source URLs and checksums):
- pain.001.001.09 (default): the version of the current EPC SEPA Credit Transfer customer-to-PSP implementation
  guidelines and of Finance Finland's "ISO 20022 Payments Guide" (2025). The same guide says Finnish banks still take
  pain.001.001.03 on many channels, so the version is a parameter: env SEPA_PAIN001_VERSION=pain.001.001.03 switches
  the generator (both versions validate against their official XSD). The matching status report is pain.002.001.10
  (or .001.03); camt.054 is read in .001.08 and .001.02 (what Finnish banks deliver today).

Finnish rules applied (Finance Finland guide 2025 sections 2.3, 3, 5):
- InitgPty / Dbtr identification: bank-assigned payer service code in Othr/Id with SchmeNm/Cd "BANK" (env
  BANK_SERVICE_CODE), business ID (Y-tunnus, 8 digits) as a repeated Othr with Issr "YTJ".
- Creditor reference in RmtInf/Strd/CdtrRefInf with Tp/CdOrPrtry/Cd "SCOR": Finnish reference number without Issr,
  RF creditor reference (ISO 11649) with Issr "ISO". Otherwise RmtInf/Ustrd with the invoice number.
- SEPA block (EUR, both accounts in the SEPA area): PmtTpInf/SvcLvl/Cd SEPA, ChrgBr SLEV. Anything else (other
  currency or creditor outside SEPA): no SEPA service level, ChrgBr SHAR.
- Identifiers (MsgId, PmtInfId, InstrId, EndToEndId) use A-Z, 0-9 and '-' only, max 35, no leading '/' or '//'.

Every IBAN (mod 97 + country length) and BIC (ISO 9362) is checked before a file is built; a wrong one raises
PaymentError with a plain sentence. Every generated file is validated against the official XSD before it is returned.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from lxml import etree

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "data" / "sepa"
PAIN001_VERSIONS = ("pain.001.001.09", "pain.001.001.03")
PAIN002_FOR = {"pain.001.001.09": "pain.002.001.10", "pain.001.001.03": "pain.002.001.03"}
PAIN002_VERSIONS = ("pain.002.001.10", "pain.002.001.03")
CAMT054_VERSIONS = ("camt.054.001.08", "camt.054.001.02")
HELSINKI = ZoneInfo("Europe/Helsinki")


def default_version() -> str:
    v = os.environ.get("SEPA_PAIN001_VERSION", "pain.001.001.09").strip() or "pain.001.001.09"
    return v if v in PAIN001_VERSIONS else "pain.001.001.09"


class PaymentError(ValueError):
    """Input that would make a payment file the bank rejects (wrong IBAN, BIC, amount, reference)."""

    def __init__(self, en: str, fi: str | None = None, *, code: str = "invalid", subject: str | None = None):
        super().__init__(en)
        self.en, self.fi, self.code, self.subject = en, fi or en, code, subject


# ------------------------------------------------------------------------------------------------ IBAN / BIC
# IBAN lengths per country (SWIFT IBAN Registry, release 99). Countries not listed are checked by mod 97 only.
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29, "BY": 28,
    "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18,
    "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22,
    "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20,
    "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15,
    "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SC": 31, "SE": 24, "SI": 19,
    "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}
# Countries and territories in the geographical scope of the EPC SEPA schemes (EPC409-09 list of countries).
SEPA_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
    "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH", "GB", "SM", "VA", "MC", "AD", "GI",
    "AL", "MD", "ME", "MK",
}
ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "ISK", "CLP", "VND", "XOF", "XAF", "UGX", "PYG"}
BIC_RE = re.compile(r"^[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")  # BICFIDec2014Identifier (ISO 9362)
ID_RE = re.compile(r"[^A-Za-z0-9-]")


def normalise_iban(iban: str | None) -> str:
    return re.sub(r"\s+", "", str(iban or "")).upper()


def iban_valid(iban: str | None) -> bool:
    s = normalise_iban(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    if s[:2] in IBAN_LENGTHS and len(s) != IBAN_LENGTHS[s[:2]]:
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


def bic_valid(bic: str | None) -> bool:
    return bool(BIC_RE.fullmatch(str(bic or "").strip().upper()))


def fi_reference_valid(ref: str) -> bool:
    """Finnish national reference (viitenumero): 4-20 digits, last = 7-3-1 check digit."""
    s = re.sub(r"\s+", "", ref or "")
    if not re.fullmatch(r"\d{4,20}", s):
        return False
    body, check = s[:-1], int(s[-1])
    weights = (7, 3, 1)
    total = sum(int(d) * weights[i % 3] for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def rf_reference_valid(ref: str) -> bool:
    """ISO 11649 creditor reference: RF + 2 check digits + up to 21 alphanumerics, mod 97 == 1."""
    s = re.sub(r"\s+", "", ref or "").upper()
    if not re.fullmatch(r"RF\d{2}[A-Z0-9]{1,21}", s):
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def reference_kind(ref: str | None) -> str | None:
    """'rf', 'fi' or None (no reference). Raises PaymentError for a reference that fails its check digit."""
    s = re.sub(r"\s+", "", ref or "").upper()
    if not s:
        return None
    if s.startswith("RF"):
        if rf_reference_valid(s):
            return "rf"
        raise PaymentError(f"RF reference {ref} fails its ISO 11649 check digits; ask the supplier for the right one.",
                           f"RF-viite {ref} ei läpäise tarkistetta; pyydä toimittajalta oikea viite.", code="reference")
    if fi_reference_valid(s):
        return "fi"
    raise PaymentError(f"Reference number {ref} is not a valid Finnish reference (7-3-1 check digit) or RF reference.",
                       f"Viitenumero {ref} ei ole kelvollinen suomalainen viite tai RF-viite.", code="reference")


def is_sepa(currency: str, creditor_iban: str | None, debtor_iban: str | None) -> bool:
    return (currency or "").upper() == "EUR" and bool(creditor_iban) and \
        normalise_iban(creditor_iban)[:2] in SEPA_COUNTRIES and normalise_iban(debtor_iban)[:2] in SEPA_COUNTRIES


def format_amount(cents: int, currency: str) -> str:
    cents = int(cents)
    if cents <= 0:
        raise PaymentError(f"Amount must be positive (got {cents} cents).", f"Summan pitää olla positiivinen ({cents} senttiä).",
                           code="amount")
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        if cents % 100:
            raise PaymentError(f"{currency} has no minor unit; {cents} cents is not a whole amount.",
                               f"Valuutassa {currency} ei ole desimaaleja; {cents} senttiä ei ole kokonaisluku.", code="amount")
        return str(cents // 100)
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def amount_decimal(cents: int, currency: str) -> Decimal:
    return Decimal(format_amount(cents, currency))


def clean_id(value: str, max_len: int = 35) -> str:
    s = ID_RE.sub("", str(value or ""))[:max_len].lstrip("/")
    if not s:
        raise PaymentError("An ISO 20022 identifier is empty after removing characters banks do not accept.",
                           code="identifier")
    return s


def _text(value, max_len: int) -> str:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s[:max_len]


def business_id_digits(ytunnus: str | None) -> str:
    """Y-tunnus 1234567-8 -> 12345678 (format used in the Finnish guide examples)."""
    d = re.sub(r"\D", "", ytunnus or "")
    return d.rjust(8, "0") if d else ""


def parse_company_address(address: str | None, country: str = "FI") -> dict:
    """'Mannerheimintie 1, 00100 Helsinki, Finland' -> {street, postal_code, city, country}. Hybrid address form:
    town + country always (EPC address guidance), street when present."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if parts and parts[-1].lower() in ("finland", "suomi", "fi"):
        parts = parts[:-1]
    out = {"country": country}
    if parts:
        last = parts.pop()
        m = re.fullmatch(r"(\d{5})\s+(.+)", last)
        if m:
            out["postal_code"], out["city"] = m.group(1), m.group(2)
        else:
            out["city"] = last
    if parts:
        out["street"] = ", ".join(parts)
    return out


# ------------------------------------------------------------------------------------------------ schemas
@lru_cache(maxsize=None)
def schema(name: str) -> etree.XMLSchema:
    path = SCHEMA_DIR / f"{name}.xsd"
    if not path.exists():
        raise FileNotFoundError(f"Official schema {name}.xsd missing from {SCHEMA_DIR}")
    return etree.XMLSchema(etree.parse(str(path)))


def message_name(xml_bytes: bytes) -> str:
    """'pain.002.001.10' etc. from the Document namespace."""
    root = etree.fromstring(xml_bytes, parser=_parser())
    ns = etree.QName(root).namespace or ""
    m = re.search(r"xsd:([a-z]{4}\.\d{3}\.\d{3}\.\d{2})$", ns)
    if not m:
        raise PaymentError(f"Not an ISO 20022 message (namespace {ns or 'none'}).", f"Ei ISO 20022 -sanoma ({ns}).",
                           code="format")
    return m.group(1)


def validate(xml_bytes: bytes, name: str | None = None) -> list[str]:
    """Validate against the official XSD. Returns [] when valid, else the schema errors (line: message)."""
    name = name or message_name(xml_bytes)
    doc = etree.fromstring(xml_bytes, parser=_parser())
    sch = schema(name)
    if sch.validate(doc):
        return []
    return [f"line {e.line}: {e.message}" for e in sch.error_log][:50]


def _parser():
    return etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False, huge_tree=False)


# ------------------------------------------------------------------------------------------------ pain.001
def _sub(parent, tag, text=None, **attrib):
    el = etree.SubElement(parent, f"{{{parent.nsmap[None]}}}{tag}", **attrib)
    if text is not None:
        el.text = str(text)
    return el


def _postal(parent, addr: dict, country: str):
    pa = _sub(parent, "PstlAdr")
    if addr.get("street"):
        _sub(pa, "StrtNm", _text(addr["street"], 70))
    if addr.get("postal_code"):
        _sub(pa, "PstCd", _text(addr["postal_code"], 16))
    if addr.get("city"):
        _sub(pa, "TwnNm", _text(addr["city"], 35))
    _sub(pa, "Ctry", country.upper())


def _org_id(parent, *, service_code: str | None, business_id: str | None):
    others = []
    if service_code:
        others.append({"Id": _text(service_code, 35), "SchmeNm": "BANK"})
    if business_id_digits(business_id):
        others.append({"Id": business_id_digits(business_id), "Issr": "YTJ"})
    if not others:
        return
    org = _sub(_sub(parent, "Id"), "OrgId")
    for o in others:
        oth = _sub(org, "Othr")
        _sub(oth, "Id", o["Id"])
        if o.get("SchmeNm"):
            _sub(_sub(oth, "SchmeNm"), "Cd", o["SchmeNm"])
        if o.get("Issr"):
            _sub(oth, "Issr", o["Issr"])


def _agent(parent, tag: str, bic: str | None, version: str):
    fin = _sub(_sub(parent, tag), "FinInstnId")
    if bic:
        _sub(fin, "BICFI" if version == "pain.001.001.09" else "BIC", bic.upper())
    else:  # SEPA: BIC not needed for IBAN-only payments (EPC IBAN-only rule)
        _sub(_sub(fin, "Othr"), "Id", "NOTPROVIDED")


def check_debtor(debtor: dict):
    if not debtor.get("name"):
        raise PaymentError("Company name is missing (COMPANY_NAME).", "Yrityksen nimi puuttuu (COMPANY_NAME).", code="debtor")
    if not iban_valid(debtor.get("iban")):
        raise PaymentError("Our company IBAN is missing or invalid; set seller_iban in Settings (or COMPANY_IBAN).",
                           "Oman yrityksen IBAN puuttuu tai on virheellinen; aseta seller_iban asetuksissa (tai COMPANY_IBAN).",
                           code="debtor_iban")
    if not bic_valid(debtor.get("bic")):
        raise PaymentError("Our bank's BIC is missing or invalid; set seller_bic in Settings (or COMPANY_BIC).",
                           "Oman pankin BIC puuttuu tai on virheellinen; aseta seller_bic asetuksissa (tai COMPANY_BIC).",
                           code="debtor_bic")


def check_payment(p: dict, debtor_iban: str) -> dict:
    """Normalise one payment and refuse what a bank would reject. Returns the normalised payment with category."""
    cr = dict(p.get("creditor") or {})
    who = cr.get("name") or "supplier"
    if not _text(cr.get("name"), 70):
        raise PaymentError("Supplier name is missing.", "Toimittajan nimi puuttuu.", code="creditor", subject=p.get("id"))
    cur = (p.get("currency") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", cur):
        raise PaymentError(f"Currency '{p.get('currency')}' for {who} is not an ISO 4217 code.",
                           f"Valuutta '{p.get('currency')}' ({who}) ei ole ISO 4217 -koodi.", code="currency", subject=p.get("id"))
    iban = normalise_iban(cr.get("iban"))
    account = re.sub(r"\s+", "", str(cr.get("account") or ""))
    if iban:
        if not iban_valid(iban):
            raise PaymentError(f"IBAN {cr.get('iban')} of {who} is invalid (check digits or length); fix it before paying.",
                               f"Toimittajan {who} IBAN {cr.get('iban')} on virheellinen (tarkiste tai pituus); korjaa ennen maksua.",
                               code="iban", subject=p.get("id"))
    elif not account:
        raise PaymentError(f"{who} has no bank account (IBAN) on the invoice.", f"Toimittajalla {who} ei ole tilinumeroa (IBAN).",
                           code="iban", subject=p.get("id"))
    bic = str(cr.get("bic") or "").strip().upper().replace(" ", "")
    if bic and not bic_valid(bic):
        raise PaymentError(f"BIC {cr.get('bic')} of {who} is not a valid ISO 9362 BIC.",
                           f"Toimittajan {who} BIC {cr.get('bic')} ei ole kelvollinen.", code="bic", subject=p.get("id"))
    category = "sepa" if is_sepa(cur, iban, debtor_iban) else "foreign"
    if category == "foreign" and not bic:
        raise PaymentError(f"{who}: a {cur} payment outside the SEPA scheme needs the supplier bank's BIC.",
                           f"{who}: {cur}-maksu SEPAn ulkopuolelle tarvitsee saajan pankin BIC-koodin.", code="bic",
                           subject=p.get("id"))
    country = (cr.get("country") or (iban[:2] if iban else "") or (bic[4:6] if bic else "")).upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise PaymentError(f"{who}: supplier country is missing.", f"{who}: toimittajan maa puuttuu.", code="creditor",
                           subject=p.get("id"))
    kind = reference_kind(p.get("reference"))
    amount = format_amount(p["amount_cents"], cur)
    return {**p, "currency": cur, "category": category, "amount": amount, "reference_kind": kind,
            "reference_clean": re.sub(r"\s+", "", p.get("reference") or "").upper() or None,
            "creditor": {**cr, "iban": iban or None, "account": None if iban else account, "bic": bic or None,
                         "country": country}}


def build_pain001(*, msg_id: str, debtor: dict, payments: list[dict], execution_date: str,
                  created_at: datetime | None = None, version: str | None = None, batch_booking: bool = True) -> tuple[bytes, dict]:
    """Build one pain.001 credit-transfer file.

    debtor: {name, iban, bic, business_id, service_code?, address{street, postal_code, city, country}}
    payments: [{id, end_to_end_id, amount_cents, currency, creditor{name, iban|account, bic, country, address{}},
                reference?, invoice_number?, message?}]
    Returns (xml_bytes, summary) where summary = {version, msg_id, nb_of_txs, ctrl_sum, blocks[...]}.
    Raises PaymentError for invalid input and ValueError when the result does not validate against the XSD."""
    version = version or default_version()
    if version not in PAIN001_VERSIONS:
        raise PaymentError(f"Unsupported pain.001 version {version}; use one of {', '.join(PAIN001_VERSIONS)}.",
                           code="version")
    if not payments:
        raise PaymentError("A payment file needs at least one payment.", "Maksuaineistossa pitää olla vähintään yksi maksu.",
                           code="empty")
    check_debtor(debtor)
    debtor_iban = normalise_iban(debtor["iban"])
    checked = [check_payment(p, debtor_iban) for p in payments]
    seen = set()
    for p in checked:
        p["end_to_end_id"] = clean_id(p["end_to_end_id"])
        if p["end_to_end_id"] in seen:
            raise PaymentError(f"EndToEndId {p['end_to_end_id']} appears twice in one file.", code="identifier")
        seen.add(p["end_to_end_id"])
    msg_id = clean_id(msg_id)
    created = (created_at or datetime.now(HELSINKI)).astimezone(HELSINKI).replace(microsecond=0)

    groups: dict[tuple, list] = {}
    for p in checked:
        groups.setdefault((execution_date, p["currency"], p["category"]), []).append(p)

    ns = f"urn:iso:std:iso:20022:tech:xsd:{version}"
    root = etree.Element(f"{{{ns}}}Document", nsmap={None: ns})
    init = _sub(root, "CstmrCdtTrfInitn")
    hdr = _sub(init, "GrpHdr")
    total = sum((Decimal(p["amount"]) for p in checked), Decimal(0))
    _sub(hdr, "MsgId", msg_id)
    _sub(hdr, "CreDtTm", created.isoformat())
    _sub(hdr, "NbOfTxs", len(checked))
    _sub(hdr, "CtrlSum", f"{total:.2f}")
    ip = _sub(hdr, "InitgPty")
    _sub(ip, "Nm", _text(debtor["name"], 70))
    _org_id(ip, service_code=debtor.get("service_code"), business_id=debtor.get("business_id"))

    blocks = []
    for i, ((exec_date, cur, category), items) in enumerate(sorted(groups.items(), key=lambda kv: (kv[0][2] != "sepa", kv[0][1])), 1):
        block_sum = sum((Decimal(p["amount"]) for p in items), Decimal(0))
        pmt_inf_id = clean_id(f"{msg_id[:30]}-B{i}")
        pi = _sub(init, "PmtInf")
        _sub(pi, "PmtInfId", pmt_inf_id)
        _sub(pi, "PmtMtd", "TRF")
        _sub(pi, "BtchBookg", "true" if batch_booking else "false")
        _sub(pi, "NbOfTxs", len(items))
        _sub(pi, "CtrlSum", f"{block_sum:.2f}")
        if category == "sepa":
            _sub(_sub(_sub(pi, "PmtTpInf"), "SvcLvl"), "Cd", "SEPA")
        if version == "pain.001.001.09":
            _sub(_sub(pi, "ReqdExctnDt"), "Dt", exec_date)
        else:
            _sub(pi, "ReqdExctnDt", exec_date)
        dbtr = _sub(pi, "Dbtr")
        _sub(dbtr, "Nm", _text(debtor["name"], 70))
        addr = debtor.get("address") or {}
        _postal(dbtr, addr, addr.get("country") or "FI")
        _org_id(dbtr, service_code=debtor.get("service_code"), business_id=debtor.get("business_id"))
        _sub(_sub(_sub(pi, "DbtrAcct"), "Id"), "IBAN", debtor_iban)
        _agent(pi, "DbtrAgt", debtor["bic"], version)
        _sub(pi, "ChrgBr", "SLEV" if category == "sepa" else "SHAR")
        for n, p in enumerate(items, 1):
            tx = _sub(pi, "CdtTrfTxInf")
            pid = _sub(tx, "PmtId")
            _sub(pid, "InstrId", clean_id(p.get("instr_id") or p["end_to_end_id"]))
            _sub(pid, "EndToEndId", p["end_to_end_id"])
            amt = _sub(tx, "Amt")
            _sub(amt, "InstdAmt", p["amount"], Ccy=cur)
            cr = p["creditor"]
            if cr.get("bic") or category == "foreign":
                _agent(tx, "CdtrAgt", cr.get("bic"), version)
            c = _sub(tx, "Cdtr")
            _sub(c, "Nm", _text(cr["name"], 70))
            _postal(c, cr.get("address") or {}, cr["country"])
            acct_id = _sub(_sub(tx, "CdtrAcct"), "Id")
            if cr.get("iban"):
                _sub(acct_id, "IBAN", cr["iban"])
            else:
                _sub(_sub(acct_id, "Othr"), "Id", _text(cr["account"], 34))
            rmt = _sub(tx, "RmtInf")
            if p["reference_kind"]:
                cri = _sub(_sub(rmt, "Strd"), "CdtrRefInf")
                tp = _sub(cri, "Tp")
                _sub(_sub(tp, "CdOrPrtry"), "Cd", "SCOR")
                if p["reference_kind"] == "rf":
                    _sub(tp, "Issr", "ISO")
                _sub(cri, "Ref", p["reference_clean"])
            else:
                text = p.get("message") or (f"Invoice {p['invoice_number']}" if p.get("invoice_number") else "")
                _sub(rmt, "Ustrd", _text(text or p["end_to_end_id"], 140))
            p["pmt_inf_id"] = pmt_inf_id
        blocks.append({"pmt_inf_id": pmt_inf_id, "execution_date": exec_date, "currency": cur, "category": category,
                       "count": len(items), "ctrl_sum": f"{block_sum:.2f}",
                       "charge_bearer": "SLEV" if category == "sepa" else "SHAR",
                       "items": [{"id": p.get("id"), "end_to_end_id": p["end_to_end_id"]} for p in items]})

    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    errors = validate(xml, version)
    if errors:
        raise ValueError(f"Generated {version} does not validate against the official XSD: {errors[:3]}")
    return xml, {"version": version, "msg_id": msg_id, "created_at": created.isoformat(), "nb_of_txs": len(checked),
                 "ctrl_sum": f"{total:.2f}", "blocks": blocks, "xsd_valid": True}


# ------------------------------------------------------------------------------------------------ pain.002
STATUS_REASONS = {  # ISO 20022 ExternalStatusReason1Code (most common for credit transfers)
    "AC01": ("Incorrect account number", "Virheellinen tilinumero"),
    "AC04": ("Closed account number", "Tili on lopetettu"),
    "AC06": ("Blocked account", "Tili on estetty"),
    "AG01": ("Transaction forbidden on this account", "Tapahtuma kielletty tällä tilillä"),
    "AG02": ("Invalid bank operation code", "Virheellinen toimintokoodi"),
    "AM02": ("Amount exceeds the allowed maximum", "Summa ylittää sallitun enimmäismäärän"),
    "AM04": ("Insufficient funds", "Tilillä ei ole riittävästi katetta"),
    "AM05": ("Duplicate payment", "Kaksoismaksu"),
    "BE04": ("Creditor address missing or incorrect", "Saajan osoite puuttuu tai on virheellinen"),
    "BE05": ("Unrecognised initiating party", "Tuntematon maksun lähettäjä"),
    "CURR": ("Incorrect currency", "Virheellinen valuutta"),
    "DT01": ("Invalid date", "Virheellinen päivämäärä"),
    "DUPL": ("Duplicate payment", "Kaksoismaksu"),
    "FF01": ("Invalid file format", "Virheellinen tiedostomuoto"),
    "MS02": ("Not specified reason by customer", "Asiakkaan määrittelemätön syy"),
    "MS03": ("Not specified reason by agent", "Pankin määrittelemätön syy"),
    "NARR": ("See narrative", "Katso lisätieto"),
    "RC01": ("Bank identifier (BIC) incorrect", "Virheellinen pankin tunniste (BIC)"),
    "RR01": ("Missing debtor account or identification (regulatory)", "Maksajan tili tai tunniste puuttuu (sääntely)"),
    "RR02": ("Missing debtor name or address (regulatory)", "Maksajan nimi tai osoite puuttuu (sääntely)"),
    "RR03": ("Missing creditor name or address (regulatory)", "Saajan nimi tai osoite puuttuu (sääntely)"),
    "RR04": ("Regulatory reason", "Sääntelyyn liittyvä syy"),
}
ACCEPTED = {"ACCP", "ACSP", "ACTC", "ACWC", "ACFC", "ACIS", "ACWP", "ACCC", "RCVD", "PDNG", "PART"}


def _t(el, path: str, ns: dict) -> str | None:
    found = el.find(path, ns)
    return found.text.strip() if found is not None and found.text else None


def _reasons(el, ns: dict) -> list[dict]:
    out = []
    for r in el.findall("d:StsRsnInf", ns):
        code = _t(r, "d:Rsn/d:Cd", ns)
        prtry = _t(r, "d:Rsn/d:Prtry", ns)
        info = " ".join(x.text.strip() for x in r.findall("d:AddtlInf", ns) if x.text)
        en, fi = STATUS_REASONS.get(code or "", (None, None))
        out.append({"code": code or prtry, "proprietary": bool(prtry and not code), "info": info or None,
                    "text_en": en, "text_fi": fi})
    return out


def parse_pain002(xml_bytes: bytes) -> dict:
    """Parse a validated pain.002 (.001.10 or .001.03) into group, payment-information and transaction statuses.
    A transaction without its own status inherits its block's, which inherits the group status."""
    name = message_name(xml_bytes)
    if name not in PAIN002_VERSIONS:
        raise PaymentError(f"Expected a pain.002 status report, got {name}.", f"Odotettiin pain.002-sanomaa, saatiin {name}.",
                           code="format")
    ns = {"d": f"urn:iso:std:iso:20022:tech:xsd:{name}"}
    root = etree.fromstring(xml_bytes, parser=_parser())
    rpt = root.find("d:CstmrPmtStsRpt", ns)
    og = rpt.find("d:OrgnlGrpInfAndSts", ns)
    group_status = _t(og, "d:GrpSts", ns)
    out = {"version": name, "msg_id": _t(rpt, "d:GrpHdr/d:MsgId", ns), "created_at": _t(rpt, "d:GrpHdr/d:CreDtTm", ns),
           "orgnl_msg_id": _t(og, "d:OrgnlMsgId", ns), "orgnl_msg_nm_id": _t(og, "d:OrgnlMsgNmId", ns),
           "group_status": group_status, "group_reasons": _reasons(og, ns), "blocks": [], "transactions": []}
    for pb in rpt.findall("d:OrgnlPmtInfAndSts", ns):
        pid = _t(pb, "d:OrgnlPmtInfId", ns)
        pst = _t(pb, "d:PmtInfSts", ns)
        preasons = _reasons(pb, ns)
        out["blocks"].append({"orgnl_pmt_inf_id": pid, "status": pst or group_status, "reasons": preasons or out["group_reasons"]})
        for tx in pb.findall("d:TxInfAndSts", ns):
            st = _t(tx, "d:TxSts", ns)
            out["transactions"].append({
                "orgnl_pmt_inf_id": pid, "orgnl_end_to_end_id": _t(tx, "d:OrgnlEndToEndId", ns),
                "orgnl_instr_id": _t(tx, "d:OrgnlInstrId", ns), "status": st or pst or group_status,
                "reasons": _reasons(tx, ns) or preasons or out["group_reasons"],
                "status_id": _t(tx, "d:StsId", ns),
                "acceptance_at": _t(tx, "d:AccptncDtTm", ns)})
    return out


# ------------------------------------------------------------------------------------------------ camt.054
def _cents(text: str | None, currency: str) -> int:
    d = Decimal(text or "0")
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return int(d * 100)
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_camt054(xml_bytes: bytes) -> dict:
    """Entries of a camt.054 (.001.08 or .001.02): credit/debit, booked amount, booking date and the transactions with
    EndToEndId and instructed amount (the original currency amount when the account is debited in EUR)."""
    name = message_name(xml_bytes)
    if name not in CAMT054_VERSIONS:
        raise PaymentError(f"Expected a camt.054 notification, got {name}.", f"Odotettiin camt.054-sanomaa, saatiin {name}.",
                           code="format")
    ns = {"d": f"urn:iso:std:iso:20022:tech:xsd:{name}"}
    root = etree.fromstring(xml_bytes, parser=_parser())
    body = root.find("d:BkToCstmrDbtCdtNtfctn", ns)
    out = {"version": name, "msg_id": _t(body, "d:GrpHdr/d:MsgId", ns), "entries": []}
    for nt in body.findall("d:Ntfctn", ns):
        acct_iban = _t(nt, "d:Acct/d:Id/d:IBAN", ns)
        acct_ccy = _t(nt, "d:Acct/d:Ccy", ns)
        for e in nt.findall("d:Ntry", ns):
            amt_el = e.find("d:Amt", ns)
            ccy = amt_el.get("Ccy")
            status = _t(e, "d:Sts/d:Cd", ns) or _t(e, "d:Sts", ns)
            booked = _t(e, "d:BookgDt/d:Dt", ns) or (_t(e, "d:BookgDt/d:DtTm", ns) or "")[:10] or None
            entry = {"account_iban": acct_iban, "account_currency": acct_ccy, "credit_debit": _t(e, "d:CdtDbtInd", ns),
                     "amount_cents": _cents(amt_el.text, ccy), "currency": ccy, "status": status, "booked_at": booked,
                     "entry_ref": _t(e, "d:NtryRef", ns), "transactions": []}
            for td in e.findall("d:NtryDtls/d:TxDtls", ns):
                tx = {"end_to_end_id": _t(td, "d:Refs/d:EndToEndId", ns), "msg_id": _t(td, "d:Refs/d:MsgId", ns),
                      "pmt_inf_id": _t(td, "d:Refs/d:PmtInfId", ns), "credit_debit": _t(td, "d:CdtDbtInd", ns)}
                tx_amt = td.find("d:Amt", ns)
                if tx_amt is None:
                    tx_amt = td.find("d:AmtDtls/d:TxAmt/d:Amt", ns)
                if tx_amt is not None:
                    tx["amount_cents"], tx["currency"] = _cents(tx_amt.text, tx_amt.get("Ccy")), tx_amt.get("Ccy")
                inst = td.find("d:AmtDtls/d:InstdAmt/d:Amt", ns)
                if inst is not None:
                    tx["instructed_cents"], tx["instructed_currency"] = _cents(inst.text, inst.get("Ccy")), inst.get("Ccy")
                entry["transactions"].append(tx)
            out["entries"].append(entry)
    return out
