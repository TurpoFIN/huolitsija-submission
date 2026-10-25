"""Finnish Customs gateways: official business XML and v1.21 message exchange."""
from __future__ import annotations

import io
import logging
import re
from abc import ABC, abstractmethod

from lxml import etree

from core import config
from core.db import db
from core.util import NO_ID, new_id, now_iso

log = logging.getLogger("tulli")

from services.customs_messages.tulli_import import TEMPLATE_VERSION
TEMPLATE_NOTES = ["Official Tulli FI415B: import MIG 2.1.7, XSD 3.0.37; runtime schema validation."]
PACKAGE_CODES = {"carton": "CT", "cartons": "CT", "ctn": "CT", "ctns": "CT", "box": "BX", "boxes": "BX", "pallet": "PX",
                 "pallets": "PX", "plt": "PX", "case": "CS", "cases": "CS", "crate": "CR", "crates": "CR", "bag": "BG",
                 "bags": "BG", "drum": "DR", "drums": "DR", "roll": "RO", "rolls": "RO", "coil": "CL", "coils": "CL",
                 "bundle": "BE", "bundles": "BE", "package": "PK", "packages": "PK", "pieces": "PK", "pcs": "PK",
                 "container": "CN", "bale": "BL", "bales": "BL", "sack": "SA", "sacks": "SA"}


def package_code(t: str | None) -> str | None:
    if not t:
        return None
    s = str(t).strip().lower()
    if re.fullmatch(r"[A-Z0-9]{2}", str(t).strip()):
        return str(t).strip()
    return PACKAGE_CODES.get(s) or PACKAGE_CODES.get(s.rstrip("s"))


def _money(cents: int | None) -> str | None:
    return None if cents is None else f"{cents / 100:.2f}"


# ---------------------------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------------------------
def build_message(decl: dict, environment: str = "MANUAL") -> str:
    from services.customs_messages.tulli_import import build_message as build
    return build(decl, environment)


# ---------------------------------------------------------------------------------------------
# Declaration summary PDF (for the customer and for manual filing)
# ---------------------------------------------------------------------------------------------
def _duty_gap_reason(item: dict) -> str:
    """Explain absent duty without claiming a lookup happened on legacy records."""
    tariff = item.get('taric') or {}
    reasons = ' '.join(r.get('en') or r.get('fi') or '' for r in tariff.get('reasons') or []).strip()
    if tariff.get('status') == 'unavailable':
        return 'TARIC lookup failed: ' + (tariff.get('error') or 'provider did not return a result.')
    if reasons:
        return reasons
    if not tariff:
        return 'No TARIC lookup result is recorded. A TARIC calculation is required.'
    if tariff.get('status') == 'needs_code':
        return 'Choose a CN or TARIC code before looking up duty.'
    return 'TARIC has not produced a duty amount; review the item inputs and tariff conditions.'


def render_summary_pdf(decl: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=10)
    norm = ParagraphStyle("n", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=12)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontName="Helvetica", fontSize=15, leading=19, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=10, leading=13,
                        spaceBefore=8, spaceAfter=3)
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Import declaration {decl.get('lrn')}",
                            author=config.COMPANY_NAME)
    e = lambda s: (str(s) if s is not None else "—").replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
    eur = lambda c: "—" if c is None else f"{c / 100:,.2f} EUR".replace(",", " ")  # noqa: E731
    t = decl.get("totals") or {}
    story = [Paragraph(e(config.COMPANY_NAME), small),
             Paragraph(f"Import declaration summary · {e(decl.get('lrn'))}", h1),
             Paragraph(f"Status: <b>{e((decl.get('status') or '').replace('_', ' '))}</b> · MRN: {e(decl.get('mrn'))} · "
                       f"Shipment {e(decl.get('shipment_ref'))} · H1, procedure {e(decl.get('procedure_code'))} · "
                       f"{e(decl.get('representation_type'))} representation · template {TEMPLATE_VERSION}", norm),
             Paragraph("This summary is not a customs decision. Duty and VAT amounts are estimates until Tulli's "
                       "decision; the CN/TARIC code is confirmed by the broker against TARIC on the declaration date.", small),
             Spacer(1, 4)]
    imp, sel = decl.get("importer") or {}, decl.get("seller") or {}
    addr = lambda p: ", ".join(x for x in [(p.get("address") or {}).get("street") if isinstance(p.get("address"), dict)  # noqa: E731
                                           else p.get("address"), (p.get("address") or {}).get("postal_code")
                                           if isinstance(p.get("address"), dict) else None,
                                           (p.get("address") or {}).get("city") if isinstance(p.get("address"), dict)
                                           else None, p.get("country")] if x)
    rows = [["Importer (3/15)", Paragraph(f"{e(imp.get('name'))}<br/>{e(addr(imp))}<br/>EORI {e(imp.get('eori'))} · "
                                          f"VAT {e(imp.get('vat_id'))}", small)],
            ["Seller (3/24)", Paragraph(f"{e(sel.get('name'))}<br/>{e(addr(sel))}", small)],
            ["Declarant (3/17)", Paragraph(e((decl.get("declarant") or {}).get("name")), small)],
            ["Representative (3/19)", Paragraph(f"{e((decl.get('representative') or {}).get('name'))} · status "
                                                f"{e(decl.get('representative_status_code'))}", small)],
            ["Delivery terms (4/1)", f"{(decl.get('delivery_terms') or {}).get('incoterm') or '—'} "
                                     f"{(decl.get('delivery_terms') or {}).get('place') or ''}"],
            ["Invoice", f"{(decl.get('invoice') or {}).get('number') or '—'} · {(decl.get('invoice') or {}).get('date') or '—'} · "
                        f"{(decl.get('invoice') or {}).get('currency') or ''} {(decl.get('invoice') or {}).get('total') or '—'}"],
            ["Exchange rate (4/15)", f"{((decl.get('valuation') or {}).get('exchange_rate') or {}).get('rate') or '—'} "
                                     f"({((decl.get('valuation') or {}).get('exchange_rate') or {}).get('source') or '—'})"],
            ["Office (5/26) / goods at (5/23)", f"{(decl.get('office') or {}).get('code') or '—'} / {decl.get('location_of_goods') or '—'}"],
            ["Transport document", f"{((decl.get('transport') or {}).get('transport_document') or {}).get('number') or '—'} · "
                                   f"containers {', '.join((decl.get('transport') or {}).get('container_numbers') or []) or '—'}"],
            ["Import VAT", e(decl.get("vat_route_label"))]]
    tb = Table(rows, colWidths=[45 * mm, 133 * mm])
    tb.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 8), ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3)]))
    story += [Paragraph("Parties and terms", h2), tb, Paragraph("Goods items", h2)]
    head = ["#", "Description", "CN", "Origin", "Net / gross kg", "Customs value", "Duty", "VAT"]
    data = [head]
    duty_gaps = []
    for it in decl.get("items") or []:
        if it.get('duty_cents') is None:
            duty_text = 'Not calculated'
            duty_gaps.append((it.get('seq'), _duty_gap_reason(it)))
        else:
            duty_text = (f"{it['duty_rate_pct']:g} % " if it.get('duty_rate_pct') is not None else '') + eur(it['duty_cents'])
        # Legacy declarations calculated VAT with missing duty treated as zero.
        # Such an amount is not a complete VAT estimate and must not look final.
        vat_text = ('Not calculated: duty missing' if it.get('duty_cents') is None else
                    'Not calculated' if it.get('vat_cents') is None else
                    (f"{it['vat_rate_pct']:g} % " if it.get('vat_rate_pct') is not None else '') + eur(it['vat_cents']))
        data.append([str(it.get("seq")), Paragraph(e(it.get("description")), small),
                     (it.get("cn_code") or it.get("hs_code") or "—") + ("" if it.get("cn_confirmed") else " *"),
                     it.get("origin_country") or "—",
                     f"{it.get('net_kg') or 0:,.1f} / {it.get('gross_kg') or 0:,.1f}".replace(",", " "),
                     Paragraph(e(eur(it.get("customs_value_cents"))), small),
                     Paragraph(e(duty_text), small), Paragraph(e(vat_text), small)])
    incomplete_duty = bool(duty_gaps) or t.get('duty_complete') is False
    incomplete_vat = incomplete_duty or any(it.get('vat_cents') is None for it in decl.get('items') or [])
    data.append(["", "Total", "", "", f"{t.get('net_kg') or 0:,.1f} / {t.get('gross_kg') or 0:,.1f}".replace(",", " "),
                 Paragraph(e(eur(t.get("customs_value_cents"))), small),
                 Paragraph('Incomplete' if incomplete_duty else e(eur(t.get("duty_cents"))), small),
                 Paragraph('Incomplete' if incomplete_vat else e(eur(t.get("vat_cents"))), small)])
    it_tb = Table(data, colWidths=[7 * mm, 42 * mm, 17 * mm, 10 * mm, 21 * mm, 27 * mm, 27 * mm, 27 * mm], repeatRows=1)
    it_tb.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 7.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                               ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 7.5), ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
    story += [it_tb, Paragraph("* CN code not yet confirmed in TARIC.", small)]
    if duty_gaps:
        story.append(Paragraph('Duty calculation required', h2))
        for seq, reason in duty_gaps:
            story.append(Paragraph(f"Item {e(seq)}: {e(reason)}", small))
        story.append(Paragraph('Duty and VAT totals are incomplete. Missing duty is not zero. '
                               'This summary does not change the stored declaration or its authority status.', small))
    open_v = [v for v in decl.get("validations") or [] if not v.get("ok")]
    if open_v:
        story.append(Paragraph("Open checks", h2))
        for v in open_v[:30]:
            story.append(Paragraph(f"[{e(v.get('rule'))}] {'item ' + str(v['item']) + ': ' if v.get('item') else ''}"
                                   f"{e(v.get('message'))} ({e(v.get('severity'))})", small))
    ai = decl.get("ai") or {}
    story += [Paragraph("Provenance", h2),
              Paragraph(f"Drafted by {e((ai.get('drafted_by') or {}).get('name'))} at {e(ai.get('drafted_at'))}"
                        f"{' · HS model ' + e(ai.get('hs_model')) if ai.get('hs_model') else ''}. "
                        f"Approved by {e((decl.get('approved_by') or {}).get('name'))} at {e(decl.get('approved_at'))}.", small),
              Paragraph(f"Generated {now_iso()} · {e(config.COMPANY_NAME)} · Keep with the import documents for 6 years "
                        f"from the end of the release year.", small)]
    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------------------------
class CustomsGateway(ABC):
    channel = "abstract"
    mode = "disabled"

    @abstractmethod
    async def submit(self, decl: dict) -> dict:
        """-> {channel, state: sent|rejected|awaiting_manual_filing|error, mrn?, error?, note?, messages[]}"""

    async def poll(self, decl: dict) -> list[dict]:
        return []

    def status(self) -> dict:
        return {"mode": self.mode, "channel": self.channel, "template_version": getattr(self, "template_version", TEMPLATE_VERSION), "ready": True}


class ManualGateway(CustomsGateway):
    channel = "manual"
    mode = "disabled"

    async def submit(self, decl: dict) -> dict:
        from services.customs_messages.tulli_store import prepare
        xml, message = await prepare(decl, system="import", channel=self.channel)
        return {"channel": self.channel, "state": "awaiting_manual_filing", "xml_version": TEMPLATE_VERSION,
                "prepared_at": now_iso(), "xml_sha_len": len(xml),
                "note": "File this declaration in Tulli's Customs Clearance Service (asiointi.tulli.fi) using the XML "
                        "and PDF summary, then record the MRN, control and release here.",
                "messages": [{"id": new_id(), "direction": "out", "type": "prepared", "at": now_iso(),
                              "channel": self.channel, "template_version": TEMPLATE_VERSION}]}

    def status(self) -> dict:
        return {**super().status(), "ready": True,
                "description": "Manual filing in Tulli's Customs Clearance Service; MRN and release recorded by the broker"}


class MessageExchangeGateway(CustomsGateway):
    """Official v1.21 transport, gated on company registration and certificate."""
    channel = "message_exchange"

    def __init__(self, mode: str, system: str = "import"):
        from services.tulli_exchange import transport
        from services.tulli_transport import APPLICATIONS
        if system not in APPLICATIONS:
            raise ValueError('Unsupported Tulli application')
        self.mode, self.system = mode, system
        self.template_version = "1.0.90" if system == "export" else TEMPLATE_VERSION
        self.transport = transport(mode)

    def missing(self):
        return self.transport.settings.missing()

    def status(self):
        missing = self.missing()
        return {**super().status(), "ready":not missing, "system":self.system, "missing":missing,
                "description_fi":"Tullin sanomaliikenne: asiakkuus ja varmenne tarvitaan.",
                "description_en":"Tulli message exchange requires customer registration and certificate.",
                "transport_implemented":True, "transport_version":"1.21", "live_proven":False}

    async def submit(self, decl):
        from services.customs_messages import tulli_import, tulli_export
        from services.customs_messages.tulli_store import record
        from services.tulli_exchange import next_reference, upload
        if self.missing():
            return {"channel":self.channel, "state":"error", "at":now_iso(), "messages":[],
                    "error_fi":"Tullin asiakkuus tai sanomaliikenteen asetukset puuttuvat.",
                    "error":"Tulli configuration incomplete: " + ', '.join(self.missing())}
        if self.system not in ('import', 'export'):
            raise ValueError('Use submit_xml for national transit messages')
        reference = await next_reference(self.transport, self.system)
        builder = tulli_import if self.system == 'import' else tulli_export
        xml = builder.build_message({**decl, 'message_id':reference}, self.mode.upper())
        await record(decl, xml, system=self.system, channel=self.channel)
        return await upload(self.transport, self.system, decl, xml, reference)

    async def submit_xml(self, xml, *, message_type, message_id, decl):
        from services.customs_messages import ncts_fi
        from services.customs_messages.schema_codec import validate, parse_xml
        from services.tulli_exchange import next_reference, upload
        if self.system != 'transit' or message_type not in ncts_fi.OUTBOUND.values():
            raise ValueError('Unsupported raw Tulli message')
        reference = await next_reference(self.transport, self.system)
        root = parse_xml(xml)
        identification = root.find('{' + etree.QName(root).namespace + '}Identification')
        if identification is None:
            raise ValueError('Finnish transit message identification is missing')
        identification.text = reference
        xml = etree.tostring(root, encoding='UTF-8', xml_declaration=True).decode()
        validate(xml, ncts_fi.DIRECTORY / (message_type + '.xsd'))
        return await upload(self.transport, self.system, decl, xml, reference)

    async def poll(self, decl):
        from services.tulli_exchange import replies
        return await replies(self.transport, self.system, decl)

    @staticmethod
    def parse(raw, system="import"):
        from services.customs_messages.tulli_responses import parse_response
        return parse_response(raw, system)


def get_gateway(system: str = "import") -> CustomsGateway:
    if system not in ("import", "export"):
        raise ValueError("Unsupported Tulli system")
    mode = (getattr(config, "TULLI_" + system.upper() + "_MODE", None) or config.TULLI_MODE or "disabled").lower()
    if mode not in ("disabled", "sandbox", "test", "production"):
        raise ValueError("Invalid Tulli mode: " + mode)
    if mode in ("test", "production"):
        return MessageExchangeGateway(mode, system)
    if system == "export":
        from services.tulli_sandbox import ExportSandboxGateway, ExportManualGateway
        return ExportSandboxGateway() if mode == "sandbox" else ExportManualGateway()
    if mode == "sandbox":
        from services.tulli_sandbox import SandboxGateway
        return SandboxGateway()
    return ManualGateway()


async def gateway_for(decl: dict, system: str = "import") -> CustomsGateway:
    from core.sandbox import sandbox_company_id
    if await sandbox_company_id(decl.get("company_id")):
        from services.tulli_sandbox import SandboxGateway, ExportSandboxGateway
        return ExportSandboxGateway() if system == "export" else SandboxGateway()
    return get_gateway(system)


async def poll_submitted():
    """Background poll of message-exchange replies (no-op in manual mode)."""
    from core.settings import get_settings
    settings = await get_settings()
    if settings.get('autopilot_halted') or not settings.get('autopilot_enabled'):
        return
    gw = get_gateway()
    if gw.channel != "message_exchange" or not gw.status()['ready']:
        return
    from services.customs import on_released, transition

    async for decl in db.customs_declarations.find({"status": {"$in": ["submitted", "accepted", "under_control"]},
                                                     "submission.channel": "message_exchange"}, NO_ID):
        try:
            for ev in await gw.poll(decl):
                from services.tulli_exchange import acknowledge
                cur = await db.customs_declarations.find_one({"id": decl["id"]}, NO_ID)
                if ev["type"] == cur["status"]:
                    await acknowledge(ev)
                    continue
                cur = await transition(cur, ev["type"], {"type": "system", "id": "tulli", "name": "Tulli"},
                                       f"Tulli message {ev.get('raw_type')}", {"mrn": ev.get("mrn") or cur.get("mrn")})
                if ev["type"] == "released":
                    await on_released(cur, None)
                from core.events import emit
                await emit('customs.response_received', {'declaration': cur, 'response': ev, 'system': 'import'})
                await acknowledge(ev)
        except Exception:  # noqa: BLE001
            log.exception("polling Tulli for %s failed", decl.get("lrn"))


from core.jobs import job, schedule  # noqa: E402


@job('customs.poll_message_exchange', agent=True, timeout=300,
     label_fi='Tullin sanomaliikenteen vastaukset', label_en='Tulli message-exchange replies')
async def tulli_poll(j):
    await poll_submitted()


schedule('customs.poll_message_exchange', job='customs.poll_message_exchange', every_seconds=120)()
