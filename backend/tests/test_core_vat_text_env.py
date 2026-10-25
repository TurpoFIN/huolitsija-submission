"""s4 D8: the zero-rate VAT wording is configured once (VAT_TEXT_ZERO_INTL_TRANSPORT_FI/_EN) for quotes and invoices;
the quote PDF's old name VAT_TEXT_ZERO_INTL_FI/_EN still works as an alias."""
import pytest

from services import billing, quote_pdf

NAMES = ["VAT_TEXT_ZERO_INTL_TRANSPORT_FI", "VAT_TEXT_ZERO_INTL_TRANSPORT_EN", "VAT_TEXT_ZERO_INTL_FI", "VAT_TEXT_ZERO_INTL_EN"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for n in NAMES:
        monkeypatch.delenv(n, raising=False)


def _both(lang):
    return billing.legal_basis("ZERO_INTL_TRANSPORT")[lang], quote_pdf.vat_text("ZERO_INTL_TRANSPORT", lang)


def test_canonical_name_sets_quote_and_invoice(monkeypatch):
    monkeypatch.setenv("VAT_TEXT_ZERO_INTL_TRANSPORT_FI", "Hyväksytty sanamuoto")
    monkeypatch.setenv("VAT_TEXT_ZERO_INTL_TRANSPORT_EN", "Approved wording")
    assert _both("fi") == ("Hyväksytty sanamuoto", "Hyväksytty sanamuoto")
    assert _both("en") == ("Approved wording", "Approved wording")
    assert billing.legal_basis("ZERO_INTL_TRANSPORT")["status"] == "approved"


def test_old_quote_name_is_an_alias_for_both(monkeypatch):
    monkeypatch.setenv("VAT_TEXT_ZERO_INTL_FI", "Vanha nimi")
    assert _both("fi") == ("Vanha nimi", "Vanha nimi")
    assert billing.configured_vat_text("ZERO_INTL_TRANSPORT")["env"] == "VAT_TEXT_ZERO_INTL"


def test_canonical_wins_over_alias(monkeypatch):
    monkeypatch.setenv("VAT_TEXT_ZERO_INTL_FI", "Vanha nimi")
    monkeypatch.setenv("VAT_TEXT_ZERO_INTL_TRANSPORT_FI", "Uusi nimi")
    assert _both("fi") == ("Uusi nimi", "Uusi nimi")


def test_unset_keeps_each_documents_default():
    inv, quote = _both("fi")
    assert billing.legal_basis("ZERO_INTL_TRANSPORT")["status"] != "approved"
    assert inv.startswith("Veroton kansainvälinen") and quote.startswith("0 %: kansainvälinen kuljetus")
