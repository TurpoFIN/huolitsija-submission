"""Customs-hold exception titles get a fully Finnish variant; a stored variant that kept the English hold reason
(as found on live) is repaired, and a correct one is left alone. Offline."""
from services.locale_backfill import hold_variants

CAT = {"Tulli asks for the packing list: the invoice lists 52 cartons, the B/L 48.":
       "Tulli pyytää pakkausluetteloa: kauppalaskulla on 52 laatikkoa, konossementilla 48."}
REASON = next(iter(CAT))


def test_half_english_variant_is_repaired():
    rec = {"title": f"Customs hold: {REASON}", "title_i18n": {"en": f"Customs hold: {REASON}", "fi": f"Tullipysäytys: {REASON}"},
           "description": f"HUO-2026-000006 held by Finnish Customs. {REASON}", "description_i18n": None}
    out = hold_variants(rec, CAT)
    assert out["title_i18n"]["fi"] == "Tullipysäytys: " + CAT[REASON]
    assert out["description_i18n"]["fi"] == "HUO-2026-000006 on Tullin pysäyttämänä. " + CAT[REASON]


def test_correct_or_unknown_records_are_untouched():
    good = {"title": f"Customs hold: {REASON}", "title_i18n": {"en": "x", "fi": "Tullipysäytys: " + CAT[REASON]}}
    assert hold_variants(good, CAT) == {}
    edited = {"title": f"Customs hold: {REASON}", "title_i18n": {"en": "x", "fi": "Operaattorin oma otsikko"}}
    assert hold_variants(edited, CAT) == {}
    assert hold_variants({"title": "Customs hold: something new"}, CAT) == {}
