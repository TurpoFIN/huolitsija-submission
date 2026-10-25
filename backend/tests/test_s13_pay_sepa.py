"""services.sepa on its own: every generated pain.001 (both supported versions) validates against the official ISO 20022
XSD in backend/data/sepa, sums are exact, and inputs a bank would reject are refused with a plain sentence."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from lxml import etree

import test_s13_support  # noqa: F401  (puts backend/ on sys.path)
from services import sepa
from test_v4_support import fi_check_digit

XSD_DIR = Path(__file__).resolve().parent.parent / "data" / "sepa"
DEBTOR = {"name": "Huolitsija Oy", "iban": "FI21 1234 5600 0007 85", "bic": "NDEAFIHH", "business_id": "2617416-4",
          "service_code": "123456789", "address": {"street": "Mannerheimintie 1", "postal_code": "00100",
                                                    "city": "Helsinki", "country": "FI"}}


def official(name: str) -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(XSD_DIR / f"{name}.xsd")))


def payments(n: int = 25) -> list[dict]:
    out = []
    for i in range(n):
        base = str(1000 + i)
        fi = base + str(fi_check_digit(base))
        cur = "EUR" if i % 3 else "USD"
        out.append({"id": f"p{i}", "end_to_end_id": f"PR-2026-0099-{i:04d}", "amount_cents": 1 + i * 99_991,
                    "currency": cur, "reference": fi if cur == "EUR" else None, "invoice_number": f"INV{i}",
                    "creditor": {"name": f"Toimittaja {i} Åbo Oy", "iban": "DE89370400440532013000" if cur == "EUR" else "GB29NWBK60161331926819",
                                 "bic": "COBADEFFXXX" if cur == "EUR" else "NWBKGB2L", "country": "DE" if cur == "EUR" else "GB"}})
    return out


@pytest.mark.parametrize("version", ["pain.001.001.09", "pain.001.001.03"])
def test_both_versions_validate_and_sum_exactly(version):
    pays = payments()
    xml, summary = sepa.build_pain001(msg_id="PR-2026-0099-TEST", debtor=DEBTOR, payments=pays, execution_date="2026-11-02",
                                      version=version)
    schema = official(version)
    assert schema.validate(etree.fromstring(xml)), [str(e) for e in schema.error_log][:3]
    ns = {"p": f"urn:iso:std:iso:20022:tech:xsd:{version}"}
    doc = etree.fromstring(xml)
    expected = sum((Decimal(p["amount_cents"]) / 100 for p in pays), Decimal(0))
    assert doc.findtext("p:CstmrCdtTrfInitn/p:GrpHdr/p:NbOfTxs", namespaces=ns) == str(len(pays))
    assert Decimal(doc.findtext("p:CstmrCdtTrfInitn/p:GrpHdr/p:CtrlSum", namespaces=ns)) == expected
    for blk in doc.findall("p:CstmrCdtTrfInitn/p:PmtInf", ns):
        cur = {a.get("Ccy") for a in blk.iter(f"{{{ns['p']}}}InstdAmt")}
        mine = [p for p in pays if p["currency"] in cur]
        assert len(cur) == 1 and blk.findtext("p:NbOfTxs", namespaces=ns) == str(len(mine))
        assert Decimal(blk.findtext("p:CtrlSum", namespaces=ns)) == sum(Decimal(p["amount_cents"]) / 100 for p in mine)
        assert blk.findtext("p:ChrgBr", namespaces=ns) == ("SLEV" if cur == {"EUR"} else "SHAR")
    # Finnish guide: payer service code with SchmeNm/Cd BANK, business ID (8 digits) with issuer YTJ
    others = doc.findall("p:CstmrCdtTrfInitn/p:GrpHdr/p:InitgPty/p:Id/p:OrgId/p:Othr", ns)
    assert [(o.findtext("p:Id", namespaces=ns), o.findtext("p:SchmeNm/p:Cd", namespaces=ns), o.findtext("p:Issr", namespaces=ns))
            for o in others] == [("123456789", "BANK", None), ("26174164", None, "YTJ")]
    assert summary["nb_of_txs"] == len(pays) and Decimal(summary["ctrl_sum"]) == expected


@pytest.mark.parametrize("field,value,needle", [
    ("iban", "DE89370400440532013001", "IBAN"),         # check digits wrong
    ("iban", "FI211234560000078", "IBAN"),              # Finnish IBAN one digit short
    ("bic", "COBA-DEFF", "BIC"),
])
def test_bad_creditor_account_refused(field, value, needle):
    pays = payments(2)
    pays[1]["creditor"][field] = value
    with pytest.raises(sepa.PaymentError) as exc:
        sepa.build_pain001(msg_id="X1", debtor=DEBTOR, payments=pays, execution_date="2026-11-02")
    assert needle in str(exc.value) and value in str(exc.value)
    assert exc.value.fi and exc.value.fi != exc.value.en


def test_bad_debtor_and_reference_refused():
    with pytest.raises(sepa.PaymentError, match="IBAN"):
        sepa.build_pain001(msg_id="X1", debtor={**DEBTOR, "iban": "FI2112345600000786"}, payments=payments(1),
                           execution_date="2026-11-02")
    pays = payments(2)
    pays[1]["reference"] = "10013"  # 1001 + wrong check digit (right one is fi_check_digit("1001"))
    assert fi_check_digit("1001") != 3
    with pytest.raises(sepa.PaymentError, match="reference"):
        sepa.build_pain001(msg_id="X1", debtor=DEBTOR, payments=pays, execution_date="2026-11-02")
    pays[1]["reference"] = "RF00 1234"
    with pytest.raises(sepa.PaymentError, match="RF reference"):
        sepa.build_pain001(msg_id="X1", debtor=DEBTOR, payments=pays, execution_date="2026-11-02")


def test_foreign_payment_without_bic_and_fractional_yen_refused():
    pays = payments(1)
    pays[0].update(currency="USD", reference=None)
    pays[0]["creditor"]["bic"] = None
    with pytest.raises(sepa.PaymentError, match="BIC"):
        sepa.build_pain001(msg_id="X1", debtor=DEBTOR, payments=pays, execution_date="2026-11-02")
    pays = payments(1)
    pays[0].update(currency="JPY", amount_cents=1_000_050, reference=None)
    with pytest.raises(sepa.PaymentError, match="JPY"):
        sepa.build_pain001(msg_id="X1", debtor=DEBTOR, payments=pays, execution_date="2026-11-02")
    pays[0]["amount_cents"] = 1_000_000  # JPY 10 000: no decimals in the file
    xml, _ = sepa.build_pain001(msg_id="X1", debtor=DEBTOR, payments=pays, execution_date="2026-11-02")
    assert b'<InstdAmt Ccy="JPY">10000</InstdAmt>' in xml


def test_identifiers_are_cleaned_for_banks():
    pays = payments(1)
    pays[0]["end_to_end_id"] = "//PR-2026/ÄÖ-0001"
    xml, _ = sepa.build_pain001(msg_id="/PR 2026 ÅÄ", debtor=DEBTOR, payments=pays, execution_date="2026-11-02")
    doc = etree.fromstring(xml)
    ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
    e2e = doc.findtext(".//p:EndToEndId", namespaces=ns)
    msg = doc.findtext(".//p:MsgId", namespaces=ns)
    for v in (e2e, msg):
        assert not v.startswith("/") and "//" not in v and all(c.isascii() for c in v) and len(v) <= 35
