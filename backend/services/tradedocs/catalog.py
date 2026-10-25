"""What each trade document is, who receives it and on which milestone it is issued.

`KINDS[kind]`:
    prefix       number prefix (house transport documents use the 12-character carrier style HUOB26000123)
    title        {fi, en}
    modes        shipment modes the document applies to (None = every mode)
    transport    True for the documents of title / contracts of carriage (issuance is a legal act: Autopilot gate)
    issue_on     milestone (or event) that issues the final version automatically
    draft_on     milestone that issues a draft for the parties to check (None = no draft)
    to           parties that receive the document: customer, shipper, consignee, notify, carrier, delivery_carrier,
                 chamber (certificate-of-origin issuer)
    formats      structured exports besides the PDF (json is always there)
"""
from __future__ import annotations

KINDS: dict[str, dict] = {
    "booking_confirmation": {
        "prefix": "BC", "title": {"fi": "Varausvahvistus", "en": "Booking confirmation"}, "modes": None, "transport": False,
        "issue_on": "booking_confirmed", "draft_on": None, "to": ["customer"], "formats": ["json"],
    },
    "commercial_invoice": {
        "prefix": "CI", "title": {"fi": "Kauppalasku", "en": "Commercial invoice"}, "modes": None, "transport": False,
        "issue_on": "booking_confirmed", "draft_on": None, "to": ["customer"], "formats": ["json"], "export_only": True,
    },
    "packing_list": {
        "prefix": "PL", "title": {"fi": "Pakkausluettelo", "en": "Packing list"}, "modes": None, "transport": False,
        "issue_on": "booking_confirmed", "draft_on": None, "to": ["customer"], "formats": ["json"], "export_only": True,
    },
    "house_bl": {
        "prefix": "HUOB", "title": {"fi": "Huolitsijan konossementti (House B/L)", "en": "House bill of lading"},
        "modes": ["sea"], "transport": True, "issue_on": "departed", "draft_on": "booking_confirmed",
        "to": ["shipper", "consignee", "notify", "customer"], "formats": ["json", "dcsa_ebl_3"],
    },
    "master_bl": {
        "prefix": "MBLD", "title": {"fi": "Master B/L — luonnos rahdinkuljettajalle", "en": "Master B/L — draft for carrier"},
        "modes": ["sea"], "transport": True, "issue_on": None, "draft_on": "booking_confirmed",
        "to": ["carrier", "customer"], "formats": ["json"], "draft_only": True,
    },
    "hawb": {
        "prefix": "HUOA", "title": {"fi": "Huolitsijan lentorahtikirja (HAWB)", "en": "House air waybill"},
        "modes": ["air"], "transport": True, "issue_on": "received", "draft_on": "booking_confirmed",
        "to": ["shipper", "consignee", "customer", "carrier"], "formats": ["json", "one_record", "xfzb"],
    },
    "cmr": {
        "prefix": "HUOC", "title": {"fi": "CMR-rahtikirja", "en": "CMR consignment note"},
        "modes": ["road"], "transport": True, "issue_on": "picked_up", "draft_on": "booking_confirmed",
        "to": ["shipper", "consignee", "carrier", "customer"], "formats": ["json", "ecmr"],
    },
    "certificate_of_origin": {
        "prefix": "CO", "title": {"fi": "Alkuperätodistushakemus", "en": "Certificate of origin application"},
        "modes": None, "transport": False, "issue_on": "booking_confirmed", "draft_on": None,
        "to": ["customer", "chamber"], "formats": ["json"], "export_only": True,
    },
    "eur1_application": {
        "prefix": "EUR1", "title": {"fi": "EUR.1-tavaratodistuksen hakemus", "en": "EUR.1 movement certificate application"},
        "modes": None, "transport": False, "issue_on": "booking_confirmed", "draft_on": None,
        "to": ["customer"], "formats": ["json"], "export_only": True, "preferential_only": True,
    },
    "dg_declaration": {
        "prefix": "DG", "title": {"fi": "Vaarallisten aineiden ilmoitus", "en": "Dangerous goods declaration"},
        "modes": None, "transport": False, "issue_on": "booking_confirmed", "draft_on": None,
        "to": ["shipper", "carrier", "customer"], "formats": ["json"], "dg_only": True,
    },
    "arrival_notice": {
        "prefix": "AN", "title": {"fi": "Saapumisilmoitus", "en": "Arrival notice"}, "modes": None, "transport": False,
        "issue_on": "arrived", "draft_on": None, "to": ["consignee", "notify", "customer"], "formats": ["json"],
        "import_side": True,
    },
    "delivery_order": {
        "prefix": "DO", "title": {"fi": "Luovutusmääräys", "en": "Delivery order"}, "modes": None, "transport": False,
        "issue_on": "customs_released", "draft_on": None, "to": ["delivery_carrier", "consignee", "customer"],
        "formats": ["json"], "import_side": True,
    },
}

ORDER = list(KINDS)

# Shipment milestones and events that drive issuance. "booking_confirmed" = s2's booking agent confirmed the booking
# (or the shipment was created without the booking agent).
MILESTONES = ["booking_confirmed", "picked_up", "received", "departed", "arrived", "customs_released", "delivered"]

# Countries whose preferential agreement with the EU accepts an EUR.1 movement certificate (pan-Euro-Mediterranean
# convention and the EU agreements that still use EUR.1). Source: European Commission, DG TAXUD "Arrangements list"
# https://taxation-customs.ec.europa.eu/customs-4/international-affairs/origin-goods/general-aspects-preferential-origin/arrangements-list_en
EUR1_COUNTRIES = {
    "AL", "BA", "CH", "LI", "DZ", "EG", "FO", "GE", "IL", "IS", "JO", "LB", "MA", "MD", "ME", "MK", "NO", "PS",
    "RS", "SY", "TN", "TR", "UA", "XK", "CL", "MX", "ZA",
}

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "EL", "HU", "IE", "IT", "LV",
      "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}


def applies(kind: str, ctx: dict) -> tuple[bool, str | None]:
    """Whether a document of `kind` belongs to the shipment in `ctx` (data.build_context). (ok, reason when not)."""
    spec = KINDS[kind]
    mode = ctx["shipment"]["mode"]
    # The shipment's own mode decides (a road shipment's ro-ro ferry leg stays under its CMR, CMR art. 2); a
    # multimodal shipment gets the documents of its main legs.
    modes = {mode} if mode != "multimodal" else {lg["mode"] for lg in ctx["legs"] if lg.get("role") == "main"}
    if spec["modes"] and not (set(spec["modes"]) & modes):
        return False, f"{spec['title']['en']} is for {', '.join(spec['modes'])} shipments"
    if kind == "cmr" and not ctx["road_legs_international"]:
        return False, "CMR applies to international road carriage (CMR art. 1)"
    if spec.get("dg_only") and not ctx["dangerous_goods"]:
        return False, "No dangerous goods on this shipment"
    if spec.get("export_only") and ctx["origin"]["country"] in EU and ctx["destination"]["country"] in EU:
        return False, "Goods move inside the EU single market"
    if spec.get("preferential_only") and ctx["destination"]["country"] not in EUR1_COUNTRIES:
        return False, f"{ctx['destination']['country']} does not use EUR.1"
    if spec.get("preferential_only") and ctx["origin"]["country"] not in EU:
        return False, "EUR.1 is issued in the exporting EU country"
    return True, None


def auto_kinds(ctx: dict, milestone: str) -> list[str]:
    """Kinds to issue (final) or draft automatically when `milestone` happens on the shipment."""
    out = []
    direction = ctx["shipment"].get("direction")
    for kind, spec in KINDS.items():
        if spec["issue_on"] != milestone and spec.get("draft_on") != milestone:
            continue
        if not applies(kind, ctx)[0]:
            continue
        if spec.get("export_only") and direction == "import":
            continue  # on an import the supplier issues these; they come in through extraction
        out.append(kind)
    return out
