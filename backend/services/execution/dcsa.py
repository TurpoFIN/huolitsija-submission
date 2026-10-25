"""DCSA payloads and client for shipping instructions (eBL 3.0) and VGM (VGM 1.0).

Every payload is validated against the official DCSA OpenAPI schemas in backend/data/dcsa/ (copied from
dcsaorg/Conformance-Gateway: EBL_v3.0.0.yaml = spec 3.0.3, VGM_v1.0.0.yaml = 1.0.1) before it is sent anywhere,
by API or by e-mail.

Carrier connection per carrier code (go-live = env only; the carrier's DCSA endpoint from its API portal):
    CARRIER_<CODE>_SI_MODE            dcsa | email   (default EXECUTION_SI_CHANNEL_DEFAULT, else email)
    CARRIER_<CODE>_DCSA_EBL_URL       base URL that serves /v3/shipping-instructions
    CARRIER_<CODE>_DCSA_VGM_URL       base URL that serves /vgm-declarations (default: the EBL URL)
    CARRIER_<CODE>_DCSA_AUTH_HEADER   header name the carrier wants (default Authorization)
    CARRIER_<CODE>_DCSA_AUTH_VALUE    secret value (VGM: CARRIER_<CODE>_DCSA_VGM_AUTH_VALUE, default the same)
    CARRIER_<CODE>_DCSA_CALLBACK_KEY  secret the carrier sends back in X-Huolitsija-Dcsa-Key on notifications
    CARRIER_<CODE>_BOOKING_DESK_EMAIL booking/documentation desk for the e-mail channel (or carriers.booking_desk_email)
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import httpx
import yaml
from jsonschema import Draft202012Validator

from core import config

DATA = Path(__file__).resolve().parents[2] / "data" / "dcsa"
SPECS = {"ebl": "EBL_v3.0.0.yaml", "vgm": "VGM_v1.0.0.yaml", "bkg": "BKG_v2.0.0.yaml"}
API_VERSION = {"ebl": "3.0.0", "vgm": "1.0.0"}

# ISO 6346 size-type codes for the equipment names the app uses.
ISO_TYPE = {"20GP": "22G1", "20DV": "22G1", "20": "22G1", "40GP": "42G1", "40DV": "42G1", "40": "42G1", "40HC": "45G1",
            "40HQ": "45G1", "45HC": "L5G1", "20RF": "22R1", "40RF": "45R1", "40RH": "45R1", "20OT": "22U1", "40OT": "42U1",
            "20FR": "22P1", "40FR": "42P1", "20TK": "22T1"}
EU = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
# Incoterms where the seller pays the main carriage -> freight prepaid on the carrier's document.
PREPAID_TERMS = {"CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DAT", "DDP"}


@lru_cache
def spec(name: str) -> dict:
    return yaml.safe_load((DATA / SPECS[name]).read_text())


def spec_version(name: str) -> str:
    return spec(name)["info"]["version"]


def validate(name: str, component: str, payload: dict) -> list[str]:
    """Errors (path: message) of `payload` against components.schemas.<component> of the DCSA spec; [] = valid."""
    doc = spec(name)
    schema = {"$ref": f"#/components/schemas/{component}", "components": doc["components"]}
    errs = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message[:300]}" for e in errs]


# ------------------------------------------------------------------------------------------------- ISO 6346

def iso6346_valid(no: str) -> bool:
    """Container number check digit (ISO 6346 Annex A)."""
    no = (no or "").upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", no):
        return False
    values, v = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if v % 11 == 0:
            v += 1
        values[ch] = v
        v += 1
    total = sum((values[c] if c.isalpha() else int(c)) * (2 ** i) for i, c in enumerate(no[:10]))
    return total % 11 % 10 == int(no[10])


def iso_type(name: str | None) -> str | None:
    if not name:
        return None
    n = str(name).upper().replace("'", "").replace(" ", "")
    if re.fullmatch(r"[0-9L][0-9A-Z]{3}", n) and n not in ISO_TYPE:
        return n
    return ISO_TYPE.get(n)


# ------------------------------------------------------------------------------------------------- builders

def _addr(p: dict | None) -> dict | None:
    a = (p or {}).get("address") or {}
    if not a.get("city") or not a.get("country"):
        return None
    out = {"street": (a.get("street") or a.get("line1") or a["city"])[:100], "city": a["city"][:35], "countryCode": a["country"][:2].upper()}
    if a.get("postal_code"):
        out["postCode"] = str(a["postal_code"])[:10]
    return out


def _party(p: dict | None, fallback_contact: dict) -> dict | None:
    if not p or not p.get("name"):
        return None
    out = {"partyName": p["name"][:70]}
    if _addr(p):
        out["address"] = _addr(p)
    contact = {k: v for k, v in {"name": (p.get("contact_name") or p["name"])[:35], "email": p.get("email"),
                                 "phone": p.get("phone")}.items() if v}
    out["partyContactDetails"] = [contact if (contact.get("email") or contact.get("phone")) else fallback_contact]
    return out


def freight_term(incoterm: str | None, direction: str | None) -> str:
    return "PRE" if (incoterm or "").upper() in PREPAID_TERMS else "COL"


def hs6(cargo: dict) -> list[str]:
    codes = []
    for c in [cargo.get("hs_code"), *(cargo.get("hs_codes") or []), cargo.get("hs_hint")]:
        d = re.sub(r"\D", "", str(c or ""))
        if len(d) >= 6:
            codes.append(d[:10])
    return list(dict.fromkeys(codes))


def goods_lines(text: str | None) -> list[str]:
    """DCSA 3.0 descriptionOfGoods: at most 150 lines of at most 35 characters (the B/L goods column)."""
    import textwrap
    return [ln for ln in textwrap.wrap(" ".join((text or "").split()), 35)][:150] or ["General cargo"]


def build_si(record: dict, shipment: dict, *, contact: dict, document_type: str = "SWB") -> dict:
    """CreateShippingInstructions (eBL 3.0) from the execution record (containers, booking ref) and the shipment."""
    cargo = shipment.get("cargo") or {}
    parties = shipment.get("parties") or {}
    containers = record["containers"]
    eq = []
    items = []
    for c in containers:
        u = {"isShipperOwned": bool(c.get("shipper_owned")), "equipmentReference": c["equipment_reference"]}
        if c.get("iso_type"):
            u["ISOEquipmentCode"] = c["iso_type"]
        if c.get("seal_number"):
            u["seals"] = [{"number": c["seal_number"][:15]}]
        if c.get("shipping_marks"):
            u["shippingMarks"] = [m[:35] for m in c["shipping_marks"]][:50]
        eq.append(u)
        pk = {"numberOfPackages": int(c.get("packages") or 1), "description": (cargo.get("package_type") or "packages")[:100]}
        items.append({"equipmentReference": c["equipment_reference"],
                      "cargoGrossWeight": {"value": round(float(c["cargo_gross_kg"]), 1), "unit": "KGM"},
                      "outerPackaging": pk})
    item = {"carrierBookingReference": record["carrier_booking_reference"][:35],
            "descriptionOfGoods": goods_lines(cargo.get("description")),
            "HSCodes": hs6(cargo), "cargoItems": items}
    dest = ((shipment.get("destination") or {}).get("country") or (shipment.get("destination") or {}).get("country_code") or "").upper()
    doc_parties = {"shipper": _party(parties.get("shipper"), contact) or
                   {"partyName": shipment.get("customer_name", "")[:70], "partyContactDetails": [contact]}}
    if parties.get("consignee"):
        doc_parties["consignee"] = _party(parties["consignee"], contact)
    if parties.get("notify") or parties.get("notify_party"):
        doc_parties["notifyParty"] = _party(parties.get("notify") or parties.get("notify_party"), contact)
    si = {"transportDocumentTypeCode": document_type, "isShippedOnBoardType": True, "isElectronic": False, "isToOrder": False,
          "freightPaymentTermCode": freight_term(shipment.get("incoterm"), shipment.get("direction")),
          "partyContactDetails": [contact], "documentParties": doc_parties,
          "isCargoDeliveredInICS2Zone": dest in EU, "consignmentItems": [item], "utilizedTransportEquipments": eq}
    if document_type == "BOL":
        si["numberOfOriginalsWithCharges"] = 3
    refs = shipment.get("references") or {}
    if refs.get("customer_ref") or shipment.get("reference"):
        si["references"] = [{"type": "CR", "value": (refs.get("customer_ref") or shipment["reference"])[:35]}]
    return si


def build_vgm(record: dict, container: dict, shipment: dict, *, submitter: dict, signatory: str, at: str) -> dict:
    """One VGMDeclaration (VGM 1.0) for a container. SOLAS VI/2: SM1 = weighed packed container, SM2 = cargo + tare."""
    method = container["vgm"]["method"]
    gross = round(float(container["vgm"]["weight_kg"]), 1)
    shipper = (shipment.get("parties") or {}).get("shipper") or {"name": shipment.get("customer_name")}
    equipment = {"equipmentReference": container["equipment_reference"], "isShipperOwned": bool(container.get("shipper_owned")),
                 "cargoGrossWeight": {"value": round(float(container["cargo_gross_kg"]), 1), "unit": "KGM"},
                 "tareWeight": {"value": round(float(container["tare_kg"]), 1), "unit": "KGM"}}
    if container.get("iso_type"):
        equipment["ISOEquipmentCode"] = container["iso_type"]
    if container.get("seal_number"):
        equipment["seals"] = [{"number": container["seal_number"][:15]}]
    vgm = {"weight": {"value": gross, "unit": "KGM"}, "method": method, "dateTime": container["vgm"].get("weighed_at") or at}
    if container["vgm"].get("certificate"):
        vgm["certificationReference"] = container["vgm"]["certificate"][:70]
    responsible = {"partyName": (shipper.get("name") or "")[:70]}
    if _addr(shipper):
        responsible["address"] = _addr(shipper)
    return {"declarationReference": f"VGM-{record['carrier_booking_reference']}-{container['equipment_reference']}-{at}"[:500],
            "isRetracted": False, "declarationDateTime": at, "VGM": vgm, "equipmentDetails": equipment,
            "shipmentDetails": {"carrierBookingReference": record["carrier_booking_reference"][:35]},
            "responsibleParty": responsible, "authorizedPersonSignatory": signatory[:255], "submittingParty": submitter}


# ------------------------------------------------------------------------------------------------- carrier config + client

def _code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (code or "").upper()).strip("_")


def carrier_conf(code: str) -> dict:
    c = _code(code)
    e = lambda k, d="": config.env(f"CARRIER_{c}_{k}", d)  # noqa: E731
    ebl = e("DCSA_EBL_URL").rstrip("/")
    return {"code": c, "si_mode": (e("SI_MODE") or config.env("EXECUTION_SI_CHANNEL_DEFAULT", "email")).lower(),
            "ebl_url": ebl, "vgm_url": (e("DCSA_VGM_URL") or ebl).rstrip("/"),
            "auth_header": e("DCSA_AUTH_HEADER", "Authorization"), "auth_value": e("DCSA_AUTH_VALUE"),
            "vgm_auth_value": e("DCSA_VGM_AUTH_VALUE") or e("DCSA_AUTH_VALUE"),
            "callback_key": e("DCSA_CALLBACK_KEY"), "desk_email": e("BOOKING_DESK_EMAIL")}


def channel_status(code: str) -> dict:
    """What is configured for a carrier, without secrets."""
    cf = carrier_conf(code)
    return {"code": cf["code"], "si_mode": cf["si_mode"], "dcsa_ebl_url": cf["ebl_url"] or None, "dcsa_vgm_url": cf["vgm_url"] or None,
            "auth_configured": bool(cf["auth_value"]), "callback_key_configured": bool(cf["callback_key"]),
            "desk_email_env": bool(cf["desk_email"])}


class DcsaClient:
    def __init__(self, base_url: str, header: str, value: str, api: str):
        self.base, self.api = base_url.rstrip("/"), api
        self.headers = {"Content-Type": "application/json", "Accept": "application/json", "API-Version": API_VERSION[api]}
        if value:
            self.headers[header] = value

    async def _call(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> tuple[int, dict | list | None]:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, f"{self.base}{path}", headers=self.headers, json=json, params=params)
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = {"raw": r.text[:2000]}
        return r.status_code, body

    async def post_si(self, payload: dict):
        return await self._call("POST", "/v3/shipping-instructions", payload)

    async def put_si(self, ref: str, payload: dict):
        return await self._call("PUT", f"/v3/shipping-instructions/{ref}", payload)

    async def get_si(self, ref: str):
        return await self._call("GET", f"/v3/shipping-instructions/{ref}")

    async def post_vgm(self, declarations: list[dict]):
        return await self._call("POST", "/vgm-declarations", {"VGMDeclarations": declarations})


def ebl_client(code: str) -> DcsaClient:
    cf = carrier_conf(code)
    return DcsaClient(cf["ebl_url"], cf["auth_header"], cf["auth_value"], "ebl")


def vgm_client(code: str) -> DcsaClient:
    cf = carrier_conf(code)
    return DcsaClient(cf["vgm_url"], cf["auth_header"], cf["vgm_auth_value"], "vgm")
