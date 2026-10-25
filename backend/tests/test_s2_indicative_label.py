"""s2: an option priced from an indicative market rate card (no carrier contract) is labelled honestly
("Ohjeellinen hinta – vahvistetaan varauksen yhteydessä"); the label does not change firmness, bookable or any gate."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def opt(**kw):
    return {"id": "o1", "mode": "road", "carrier_name": "PostNord", "total_cents": 49008, "transit_days": 2,
            "valid_until": "2026-11-08", "firmness": "firm", "bookable": True, "rate_source": "indicative", **kw}


def test_label_indicative_marks_only_indicative_non_contract_options():
    from services.pricing import label_indicative
    out = label_indicative([opt(), opt(id="o2", rate_source="carrier_api"), opt(id="o3", price_basis="contract")])
    assert [o.get("price_status") for o in out] == ["indicative", None, None]
    assert all(o["firmness"] == "firm" and o["bookable"] is True for o in out)  # a label only


def test_quote_mail_says_indicative_price_confirmed_at_booking():
    from services.email_channel import texts
    q = {"number": "Q-1", "valid_until": "2026-11-08", "options": [opt(price_status="indicative")],
         "request": {"origin": {"name": "Helsinki"}, "destination": {"name": "Stockholm"}}}
    _title, fi_lines = texts.quote_mail(q, "fi", "https://app.example")
    _title, en_lines = texts.quote_mail(q, "en", "https://app.example")
    assert "Ohjeellinen hinta – vahvistetaan varauksen yhteydessä" in "\n".join(fi_lines)
    assert "Indicative price, confirmed at booking" in "\n".join(en_lines)
    q["options"] = [opt(rate_source="carrier_api")]
    assert "Ohjeellinen" not in "\n".join(texts.quote_mail(q, "fi", "https://app.example")[1])
    q["options"] = [opt(firmness="estimate", bookable=False)]
    fi = "\n".join(texts.quote_mail(q, "fi", "https://app.example")[1])
    assert "ARVIO, ei vielä varattavissa" in fi and "Ohjeellinen" not in fi
