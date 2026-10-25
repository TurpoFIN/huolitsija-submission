"""Document reader texts in the Finnish UI (s4 D4) and today's date in its prompt (s4 D5). Offline: no model, no DB."""
import ast
from datetime import datetime, timezone
from pathlib import Path

from routes.companies import _checklist
from services import extraction as ex
from services.kyc import fi_summary

SRC = Path(ex.__file__).read_text()


def test_prompt_states_today_in_helsinki(monkeypatch):
    class Late(datetime):  # 23:30 UTC on 24.10. is already 25.10. in Helsinki
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(ex, "datetime", Late)
    prompt = ex.build_prompt("commercial_invoice", {"method": "text", "pages": 1, "text": "INVOICE"}, "ci.pdf")
    assert prompt.startswith("Today is 2026-10-25 (Europe/Helsinki).")
    assert '{"en": "<note in English>", "fi": "<the same note in Finnish>"}' in prompt


def test_every_reader_warning_code_has_finnish():
    codes = {n.args[0].value for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_w" and isinstance(n.args[0], ast.Constant)}
    assert codes - {"model_note"} <= set(ex.WARN_FI), codes - set(ex.WARN_FI)
    fields = {"currency": {"value": "XXX", "confidence": 1}, "seller_country": {"value": "ZZ", "confidence": 0.5},
              "seller_eori": {"value": "FI1234567-8", "confidence": 1}, "subtotal_amount": {"value": 30000.0, "confidence": 1},
              "total_gross_weight_kg": {"value": 700, "confidence": 1}, "total_net_weight_kg": {"value": 742.5, "confidence": 1}}
    items = [{"description": "x", "quantity": 400, "unit_price": 18.5, "amount": 7500.0, "hs_code": "87"}]
    warnings = ex.post_check("commercial_invoice", fields, items)
    assert {w["code"] for w in warnings} >= {"missing", "currency", "country_code", "eori_check", "line_sum", "line_math",
                                             "hs_format", "origin_missing", "weights", "low_confidence"}
    for w in warnings:
        assert w["message_i18n"]["en"] == w["message"] and w["message_i18n"]["fi"] != w["message"], w
    by = {w["code"]: w["message_i18n"]["fi"] for w in warnings}
    assert by["line_sum"] == "Rivit ovat yhteensä 7 500,00, mutta välisumma on 30 000,00"
    assert by["weights"] == "Nettopaino 742,5 kg on suurempi kuin bruttopaino 700,0 kg"


def test_stored_english_warnings_get_finnish_at_read_time():
    stored = {"status": "needs_review", "reason": "Net weight 742.5 kg is more than gross weight 700.0 kg",
              "warnings": [{"code": "weights", "severity": "error", "field": "total_net_weight_kg",
                            "message": "Net weight 742.5 kg is more than gross weight 700.0 kg"},
                           {"code": "type_mismatch", "severity": "error", "field": None,
                            "message": "This looks like a packing list, not a commercial invoice. Change the type if that is right."},
                           {"code": "model_note", "severity": "info", "field": None,
                            "message": "Invoice date 2026-10-22 is in the future – verify date is correct"}]}
    out = ex.localize_extraction(stored)
    fi = [(w.get("message_i18n") or {}).get("fi") for w in out["warnings"]]
    assert fi == ["Nettopaino 742,5 kg on suurempi kuin bruttopaino 700,0 kg",
                  "Tämä näyttää olevan pakkausluettelo, ei kauppalasku. Vaihda tyyppi, jos se pitää paikkansa.",
                  None]  # free model prose from before the bilingual prompt stays as written
    assert out["reason_i18n"]["fi"] == fi[0]
    assert "message_i18n" not in stored["warnings"][0]  # read-time only, the stored record is untouched


def test_model_notes_keep_both_languages():
    notes = ex.model_notes({"warnings": [{"en": "Stamp covers the total.", "fi": "Leima peittää loppusumman."},
                                         "Page 2 is missing.", {"fi": "vain suomeksi"}, "  "]})
    assert [(n["message"], (n.get("message_i18n") or {}).get("fi")) for n in notes] == [
        ("Stamp covers the total.", "Leima peittää loppusumman."), ("Page 2 is missing.", None)]


def test_eos_reply_under_a_manual_verification_has_finnish():
    english_only = {"status": "fail", "summary": "FI3991426-4 is not in the EU EORI registry. Check the number, or apply in "
                                                 "Tulli's EORI Number Registration Service.", "checked_at": "2026-10-24T14:27:00Z"}
    company = {"eori": "FI3991426-4", "kyc": {"eori": {"status": "manual", "eori": "FI3991426-4", "summary": "x",
                                                        "summary_fi": "y", "registry_result": english_only}}}
    row = next(r for r in _checklist(company) if r["key"] == "eori")
    assert row["registry_result"]["summary_fi"] == ("FI3991426-4 ei löydy EU:n EORI-rekisteristä. Tarkista numero tai hae "
                                                    "EORI-numero Tullista.")
    assert fi_summary("eori", {"status": "pass", "eori": "FI3991426-4"}, company) == "FI3991426-4 on voimassa EU:n EORI-rekisterissä"


def test_english_only_reader_notes_are_found_and_keep_their_english():
    from services.locale_backfill import note_changes, untranslated_notes
    warnings = [{"code": "missing", "severity": "error", "message": "Invoice number was not found on the document"},
                {"code": "model_note", "severity": "info", "message": "This is a packing list"},
                {"code": "model_note", "severity": "info", "message": "Already bilingual",
                 "message_i18n": {"en": "Already bilingual", "fi": "Jo kaksikielinen"}}]
    idx = untranslated_notes({"warnings": warnings})
    assert idx == [1]
    assert note_changes(warnings, idx, [{"fi": "Tämä on pakkausluettelo", "en": "This is a packing list."}]) == {
        "extraction.warnings.1.message_i18n": {"fi": "Tämä on pakkausluettelo", "en": "This is a packing list"}}
