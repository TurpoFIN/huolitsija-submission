"""Sandbox Finnish Customs (TULLI_MODE=sandbox, or a sandbox test company in any mode).

Same code path as the real message exchange: the approved declaration is built into the versioned H1 XML by
services.tulli.build_message, "sent", accepted with an MRN (correct check digit, SBX in the serial so it can never
be mistaken for a real one), released after TULLI_SANDBOX_RELEASE_SECONDS and given a duty/VAT decision after
TULLI_SANDBOX_DECISION_SECONDS. The decision is computed the way Tulli computes it from the declared customs value
and the duty/VAT rates on the declaration, and is stored with sandbox=True. Nothing is sent to Finnish Customs.
"""
import logging
import random
from datetime import timedelta

from core import config
from core.agent_runtime import agent_bot
from core.db import db
from core.jobs import job, schedule
from core.util import NO_ID, new_id, now, now_iso, parse_iso
from services.tulli import TEMPLATE_VERSION, CustomsGateway, build_message

log = logging.getLogger("tulli_sandbox")
RELEASE_SECONDS = float(config.env("TULLI_SANDBOX_RELEASE_SECONDS", "30"))
DECISION_SECONDS = float(config.env("TULLI_SANDBOX_DECISION_SECONDS", "45"))
TULLI_ACTOR = {"type": "system", "id": "tulli_sandbox", "name": "Tulli (sandbox)"}


def sandbox_mrn(year: int | None = None) -> str:
    from services.customs import mrn_check_digit
    yy = f"{(year or now().year) % 100:02d}"
    body = f"{yy}FISBX{random.randint(0, 10**9 - 1):09d}R"  # 2 + 2 + 12 (SBX + 9 digits) + procedure letter
    return body + mrn_check_digit(body)


class SandboxGateway(CustomsGateway):
    channel = "sandbox"
    mode = "sandbox"

    async def submit(self, decl: dict) -> dict:
        from services.customs_messages.tulli_store import prepare
        xml, message = await prepare(decl, system="import", channel=self.channel)
        mrn = sandbox_mrn()
        t = now_iso()
        return {"channel": self.channel, "state": "sent", "mrn": mrn, "sandbox": True, "xml_version": TEMPLATE_VERSION,
                "xml_sha_len": len(xml), "sent_at": t,
                "note": "Sandbox: the declaration was built and accepted by the sandbox; nothing was sent to Finnish Customs.",
                "messages": [{"id": new_id(), "direction": "out", "type": "declaration", "at": t, "channel": self.channel,
                              "template_version": TEMPLATE_VERSION, "sandbox": True},
                             {"id": new_id(), "direction": "in", "type": "accepted", "at": t, "channel": self.channel,
                              "mrn": mrn, "sandbox": True}]}

    def status(self) -> dict:
        return {**super().status(), "ready": True,
                "description": "Sandbox Tulli: MRN, release and decision are simulated; nothing is sent to Finnish Customs"}


def sandbox_decision_items(decl: dict) -> list[dict]:
    """Duty = customs value x duty rate; import VAT = (customs value + duty) x VAT rate, per item, in cents."""
    out = []
    for it in decl.get("items") or []:
        value = int(it.get("customs_value_cents") or it.get("value_cents") or 0)
        duty_rate = float(it.get("duty_rate_pct") or 0)
        vat_rate = float(it.get("vat_rate_pct") if it.get("vat_rate_pct") is not None else 25.5)
        duty = int(round(value * duty_rate / 100))
        vat = int(round((value + duty) * vat_rate / 100))
        out.append({"seq": it["seq"], "duty_cents": duty, "vat_cents": vat})
    return out


async def advance(decl: dict) -> str | None:
    """Move one sandbox declaration forward when its time has come. Returns what happened."""
    from services import customs
    from services.customs_decisions import record_decision
    sub = decl.get("submission") or {}
    if sub.get("channel") != "sandbox":
        return None
    accepted_at = parse_iso(sub.get("accepted_at") or sub.get("sent_at") or sub.get("submitted_at"))
    if decl["status"] in ("accepted", "submitted", "under_control"):
        if accepted_at and now() - accepted_at >= timedelta(seconds=RELEASE_SECONDS):
            await customs.record_response(decl, "released", TULLI_ACTOR, mrn=decl.get("mrn"),
                                          note="Sandbox: released for free circulation (simulated Tulli answer)")
            return "released"
        return None
    if decl["status"] == "released" and not decl.get("customs_decision"):
        released = parse_iso(decl.get("released_at")) or accepted_at
        if released and now() - released < timedelta(seconds=DECISION_SECONDS):
            return None
        paid_by = "forwarder"
        body = {"mrn": decl["mrn"], "decision_date": now_iso()[:10], "paid_by": paid_by,
                "items": sandbox_decision_items(decl)}
        await record_decision(decl, body, agent_bot("customs_agent"))
        await db.raw.customs_declarations.update_one({"id": decl["id"]}, {"$set": {
            "customs_decision.sandbox": True,
            "customs_decision.note": "Sandbox decision computed from the declared customs value and rates"}})
        return "decision"
    return None


@job("customs.poll_tulli", max_attempts=3, timeout=180, label_fi="Tullin vastaukset", label_en="Customs answers")
async def poll_tulli(j):
    done = {}
    async for d in db.raw.customs_declarations.find(
            {"submission.channel": "sandbox", "status": {"$in": ["submitted", "accepted", "under_control", "released"]},
             "customs_decision": {"$exists": False}}, NO_ID).limit(50):
        try:
            r = await advance(d)
            if r:
                done[d.get("lrn") or d["id"]] = r
        except Exception:  # noqa: BLE001
            log.exception("sandbox Tulli step failed for %s", d.get("lrn"))
    return done


schedule("customs.poll_tulli_every_15s", job="customs.poll_tulli", every_seconds=15,
         label_fi="Tullin vastausten haku", label_en="Fetch customs answers")()


# NCTS is additive: the existing import gateway and polling contract are unchanged.
def transit_response(decl: dict, message: str) -> str:
    """Simulated P6 authority message, validated with the unmodified DG TAXUD XSD."""
    from copy import deepcopy
    from services.customs_messages import ncts
    mrn = decl.get('mrn') or transit_sandbox_mrn()
    stamp = ncts.timestamp()
    holder = {'identificationNumber': config.env('TULLI_HOLDER_EORI', config.env('TULLI_DECLARANT_ID'))}
    departure = {'referenceNumber': decl['office_of_departure']}
    destination = {'referenceNumber': decl['office_of_destination']}
    data = {'TransitOperation': {'MRN': mrn}, 'CustomsOfficeOfDeparture': departure,
            'HolderOfTheTransitProcedure': holder}
    op = data['TransitOperation']
    if message == 'CC028C':
        op.update(LRN=decl['lrn'], declarationAcceptanceDate=stamp[:10])
    elif message == 'CC029C':
        data = deepcopy(ncts.declaration_data(decl))
        data['TransitOperation'].update(MRN=mrn, declarationAcceptanceDate=stamp[:10], releaseDate=stamp[:10])
        data['Consignment'].setdefault('containerIndicator', '0')
    elif message == 'CC045C':
        op['writeOffDate'] = stamp[:10]
    elif message == 'CC009C':
        data['Invalidation'] = {'decisionDateAndTime':stamp,'decision':'1','initiatedByCustoms':'0'}
    elif message in {'CC043C','CC025C'}:
        data = {'TransitOperation':op,'CustomsOfficeOfDestinationActual':destination,
                'TraderAtDestination':holder}
        if message == 'CC043C': op.update(security='0',reducedDatasetIndicator='0')
        else: op.update(releaseDate=stamp[:10],releaseIndicator='1')
    elif message == 'CC060C':
        op.update(controlNotificationDateAndTime=stamp, notificationType='1')
    elif message == 'CC056C':
        op.update(LRN=decl['lrn'],businessRejectionType='015',rejectionDateAndTime=stamp,
                  rejectionCode='1',rejectionReason='SANDBOX rejection scenario')
    else: raise ValueError('Unsupported sandbox transit response')
    return ncts.build(message,data,sender='NTA.FI',recipient=holder['identificationNumber'])


def transit_sandbox_mrn():
    from services.customs import mrn_check_digit
    body = sandbox_mrn()[:16] + 'J'
    return body + mrn_check_digit(body)


class ExportManualGateway(CustomsGateway):
    template_version = "1.0.90"
    channel = "manual"
    mode = "disabled"

    async def submit(self, decl):
        from services.customs_messages.tulli_export import build_message, TEMPLATE_VERSION
        from services.customs_messages.tulli_store import prepare
        xml, message = await prepare(decl, system="export", channel=self.channel)
        return {"channel": self.channel, "state": "awaiting_manual_filing", "messages": [message],
                "xml_version": TEMPLATE_VERSION, "prepared_at": now_iso()}


class ExportSandboxGateway(CustomsGateway):
    template_version = "1.0.90"
    channel = "sandbox"
    mode = "sandbox"

    async def submit(self, decl):
        from services.customs import mrn_check_digit
        from services.customs_messages.tulli_export import build_message, TEMPLATE_VERSION
        from services.customs_messages.tulli_store import prepare
        xml, message = await prepare(decl, system="export", channel=self.channel)
        body = f"{now().year % 100:02d}FISBX{random.randint(0, 10**9 - 1):09d}E"
        mrn = body + mrn_check_digit(body)
        return {"channel":self.channel, "state":"sent", "mrn":mrn, "sandbox":True,
                "xml_version":TEMPLATE_VERSION, "sent_at":now_iso(), "messages":[message]}

    async def poll(self, decl):
        sub = decl.get("submission") or {}
        since = parse_iso(decl.get("released_at") if decl['status']=='released' else sub.get("sent_at"))
        delay = float(config.env("TULLI_SANDBOX_EXIT_SECONDS", "30")) if decl['status']=='released' else RELEASE_SECONDS
        if not since or now() - since < timedelta(seconds=delay):
            return []
        event = "released" if decl['status']=='accepted' else "exited" if decl['status']=='released' else None
        return [{"type":event, "mrn":decl.get("mrn"), "sandbox":True}] if event else []
