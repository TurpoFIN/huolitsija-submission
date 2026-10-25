"""The world requester persona states who handles customs only from its profile facts, in its own words; the real
quoting_agent.parse_request then decides customs_handling from that text (an exact quotation or unknown). Nothing
here sets customs_handling on the request. No model, mail or database."""
from services.demo_autoquote import requester


def test_customs_fact_reaches_the_persona_prompt_in_words():
    text = requester._profile_text({"cargo_description": "parts",
                                    "customs": "Our own customs broker arranges both the export and the import formalities"})
    assert "- Customs formalities: Our own customs broker arranges both the export and the import formalities" in text
    assert "who handles the customs formalities" in requester.SYSTEM
    assert "customs_handling" not in requester.SYSTEM and "customer_broker" not in requester.SYSTEM


def test_profile_without_a_customs_fact_says_nothing_about_customs():
    assert "Customs" not in requester._profile_text({"cargo_description": "parts", "weight_kg": "2100"})
