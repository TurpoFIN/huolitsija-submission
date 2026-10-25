"""Code lists used by the EDI messages (UN/EDIFACT D.04A and UNECE recommendations).

Transport status codes are UNECE Recommendation 24 (Trade and Transport Status Codes) as published in the
GS1 GDD TransportStatusConditionCode list (2012-03-01, "[UN/Rec24 transport status]" entries). Every STS also
carries the code's text in 4404, so a partner whose MIG uses its own codes can still read the event; a partner can
override any code with `edi_partners.status_code_map` {our milestone: code}.
"""

# UNECE Rec 19 mode of transport (TDT 8067) <-> our mode
MODE_TO_REC19 = {"sea": "1", "rail": "2", "road": "3", "air": "4", "multimodal": "6", "inland_water": "8"}
REC19_TO_MODE = {"1": "sea", "2": "rail", "3": "road", "4": "air", "6": None, "8": "sea", "9": None}

# UNCL 1225 message function code (BGM)
FN_CANCEL, FN_REPLACE, FN_ORIGINAL = "1", "5", "9"
FN_ON_HOLD, FN_NOT_ACCEPTED, FN_ACCEPTED = "23", "27", "29"

# UNCL 1001 document name code (BGM)
DOC_BOOKING_CONFIRMATION = "770"
DOC_STATUS_INFORMATION = "23"
IFTMIN_DOC_CODES = {"610", "340", "335", "700", "705"}  # forwarding instruction, shipping instruction, booking request, ...

# our milestone -> (Rec 24 code, text). Text max 35 chars (4404 an..35).
STATUS_CODES = {
    "booked": ("6E", "Booked"),
    "picked_up": ("13", "Collection/pick-up, completed"),
    "received": ("74B", "Received"),
    "export_cleared": ("12", "Cleared by customs"),
    "departed": ("24", "Departure, completed"),
    "transshipment": ("100", "Transhipment"),
    "arrived": ("1", "Arrival, completed"),
    "customs_hold": ("90", "Movement stopped"),
    "customs_released": ("12", "Cleared by customs"),
    "out_for_delivery": ("31", "En route"),
    "delivered": ("21", "Delivery, completed"),
    "exception": ("20", "Delayed, in course of transport"),
    "cancelled": ("64", "Order or request cancelled"),
}
# milestones we report by IFTSTA (booked is answered by IFTMCS)
REPORTED_MILESTONES = set(STATUS_CODES) - {"booked"}

# UN/ECE Rec 21 package type -> our package_type
PACKAGE_TYPES = {"PX": "pallet_eur", "PE": "pallet_eur", "AF": "pallet_eur", "OA": "pallet_fin", "CT": "carton", "CS": "crate",
                 "CR": "crate", "BX": "carton"}
PACKAGE_CODE_OUT = {"pallet_eur": "PX", "pallet_fin": "PX", "carton": "CT", "crate": "CR", "other": "PK"}

# ISO 6346 size/type -> our container type
ISO_CONTAINERS = {"22G1": "20GP", "22G0": "20GP", "2210": "20GP", "42G1": "40GP", "42G0": "40GP", "4310": "40GP", "45G1": "40HC",
                  "45G0": "40HC", "4510": "40HC", "L5G1": "45HC", "L5G0": "45HC", "22R1": "20RF", "45R1": "40RF", "42R1": "40RF"}
CONTAINER_ISO_OUT = {"20GP": "22G1", "40GP": "42G1", "40HC": "45G1", "45HC": "L5G1", "20RF": "22R1", "40RF": "45R1"}

INCOTERMS = {"EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP"}

# DCSA Track & Trace 2.2: our milestone -> (eventType, event type code, documentTypeCode for shipment events)
DCSA_EVENTS = {
    "booked": ("SHIPMENT", "CONF", "BKG"),
    "picked_up": ("EQUIPMENT", "PICK", None),
    "received": ("EQUIPMENT", "GTIN", None),
    "export_cleared": ("SHIPMENT", "RELS", "CUS"),
    "departed": ("TRANSPORT", "DEPA", None),
    "transshipment": ("TRANSPORT", "ARRI", None),
    "arrived": ("TRANSPORT", "ARRI", None),
    "customs_hold": ("SHIPMENT", "HOLD", "CUS"),
    "customs_released": ("SHIPMENT", "RELS", "CUS"),
    "out_for_delivery": ("EQUIPMENT", "GTOT", None),
    "delivered": ("EQUIPMENT", "DROP", None),
}
DCSA_MODE = {"sea": "VESSEL", "rail": "RAIL", "road": "TRUCK", "air": None}
