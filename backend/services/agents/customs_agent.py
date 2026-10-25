"""Customs agent: does the broker's review of an import declaration and files it through the configured Tulli
gateway (sandbox, manual or message exchange) when Autopilot and the guardrails allow.

Broker review = office of presentation from the arrival port, location of goods, the carrier's transport document
number, and confirming each item's CN code when two independent signals agree (AI classification at or above the
confidence floor, or the supplier's printed code that our nomenclature search also finds for the description).
Trade-defence (anti-dumping) items, unconfirmable codes and anything else blocking go to the owner queue. Lodging
the declaration is a legal human point unless the director has recorded a standing delegation.
A shipment with customs_handling "broker" is cleared by a person: the agent leaves its declarations alone.
"""
import logging
from datetime import timedelta

from core.agent_runtime import agent_bot, approve_as_agent, decide, policy_reason
from core.db import db
from core.events import on
from core.jobs import JobDeferred, enqueue, job
from core.settings import get_settings
from core.util import NO_ID, now, now_iso, parse_iso
from services.agents.base import agents_epoch, customer_message, entity_lock, mode_key, sweeper

log = logging.getLogger("customs_agent")
AGENT = "customs_agent"

# OFFLINE FALLBACK ONLY: legacy office guesses, never evidence of COL validity.
# Source: EU COL via tariffnumber.com/offices/fi, read 2026-10-24. Verify against Tulli's current list before go-live.
OFFICES = {
    ("FIHEL", "sea"): ("FI009801", "Helsingin Vuosaaren satama / Helsinki Vuosaari harbour"),
    ("FIVSA", "sea"): ("FI009801", "Helsingin Vuosaaren satama / Helsinki Vuosaari harbour"),
    ("FIHEL", "air"): ("FI015300", "Helsinki-Vantaa rahti / Helsinki-Vantaa cargo"),
    ("FIHEL", "road"): ("FI002000", "Tullauskeskus / Customs Clearance Centre"),
    ("FITKU", "sea"): ("FI201000", "Turun tulli / Turku customs"),
    ("FIKTK", "sea"): ("FI482000", "Kotka Hietanen"), ("FIHMN", "sea"): ("FI494600", "Haminan tulli / Hamina customs"),
    ("FIHKO", "sea"): ("FI109000", "Hangon tulli / Hanko customs"), ("FIOUL", "sea"): ("FI904000", "Oulun tulli / Oulu customs"),
    ("FIOUL", "air"): ("FI904001", "Oulun lentotulli / Oulu airport customs"),
}
DEFAULT_OFFICE = ("FI002000", "Tullauskeskus / Customs Clearance Centre")
TERMINAL = {"sea": "satamaterminaali, väliaikainen varasto / port terminal, temporary storage",
            "air": "lentorahtiterminaali, väliaikainen varasto / air cargo terminal, temporary storage",
            "road": "maantieterminaali / road terminal", "rail": "rautatieterminaali / rail terminal"}
FINAL = {"approved", "submitted", "accepted", "under_control", "released", "not_released", "invalidated", "closed"}


def _arrival(shipment: dict) -> tuple[str | None, str, dict]:
    legs = shipment.get("legs") or []
    main = [lg for lg in legs if lg.get("role") in ("main", "feeder", "onward")] or legs
    last = main[-1] if main else {}
    to = last.get("to") or shipment.get("destination") or {}
    return (to.get("unlocode") or (shipment.get("destination") or {}).get("unlocode")), (last.get("mode") or shipment.get("mode") or "sea"), last


def _transport_doc(shipment: dict) -> str | None:
    for lg in shipment.get("legs") or []:
        if lg.get("role") in ("main", None) and lg.get("transport_doc_no"):
            return lg["transport_doc_no"]
    return next((lg["transport_doc_no"] for lg in shipment.get("legs") or [] if lg.get("transport_doc_no")), None)


def _tdoc_number(decl: dict) -> str | None:
    """The transport document number as the declaration stores it (services.customs: transport.transport_document)."""
    return ((decl.get("transport") or {}).get("transport_document") or {}).get("number")


def item_confidence(it: dict) -> tuple[float, str]:
    """How sure the agent is that the item's CN code is right, and why."""
    from services import hs
    sug = it.get("hs_suggestion") or {}
    hint = (sug.get("hs_hint_check") or {})
    if not it.get("cn_code") or not it.get("hs_code"):
        return 0.0, "no CN code"
    from services import ebti
    bti = ebti.agreement(it)  # s11: an official EU BTI for the same goods is an independent second signal
    if bti and bti[0] == "disagrees":
        return 0.5, bti[1]
    if sug.get("source") == "ai" and sug.get("status") == "ok" and sug.get("confidence") is not None:
        c = float(sug["confidence"])
        if hint.get("agrees"):
            c = max(c, 0.95)
        if bti and bti[0] == "agrees":
            return max(c, 0.95), "AI classification agrees with " + bti[1]
        return c, "AI classification" + (" agrees with the supplier's code" if hint.get("agrees") else "")
    if hint.get("valid") and it["hs_code"] == (hint.get("code") or "")[:6]:
        found = [c["code"] for c in hs.search(it.get("description") or "", limit=25)]
        if it["hs_code"] in found:
            return 0.9, "supplier's code is valid and matches the goods description in the nomenclature"
        return 0.6, "supplier's code is valid but the description points elsewhere"
    return 0.4, "no independent check of the code"


async def gate_filing(decl: dict, approval: dict | None = None, *, create_approval: bool = True,
                      aeo: dict | None = None):
    """Called by services.customs.request_filing. Lodging is a legal human point unless delegated.
    `aeo` (s11): the agent's customs_aeo.filing_controls result when the caller already has it."""
    settings = await get_settings()
    rep = decl.get("representation_type") or "direct"
    legal = None
    if not settings.get("customs_filing_delegated"):
        legal = "customs_filing_not_delegated"
    elif rep == "indirect" and settings.get("customs_delegation_scope") != "direct_and_indirect":
        legal = "customs_indirect_representation"
    confs = []
    for it in decl.get("items") or []:
        if it.get("cn_confirmed_by_agent"):
            confs.append(float(it.get("cn_confidence") or 0))
        else:
            confs.append(1.0)  # confirmed by a person
    conf = min(confs) if confs else 0.0
    t = decl.get("totals") or {}
    duty = int(t.get("duty_cents") or 0)
    vat = int(t.get("vat_cents") or 0) if decl.get("vat_route") != "tilitys" else 0
    lrn = decl.get("lrn")
    ref = decl.get("shipment_ref") or ""
    extra = []
    if any(f.get("kind") == "trade_defence" and not it.get("taric_additional_codes") for it in decl.get("items") or []
           for f in it.get("flags") or []):
        extra.append(policy_reason("trade_defence", "Tuotteeseen voi kohdistua polkumyyntitulli; henkilö tarkistaa TARIC-koodin",
                                   "The goods may carry anti-dumping duty; a person checks the TARIC code"))
    # s11 AEO controls: four-eyes thresholds, segregation of duties and the POA / EORI / KYC / sanctions controls go
    # to the owner queue as reasons (the delegation itself stays the legal point above).
    from services import customs_aeo
    if aeo is None:
        aeo = await customs_aeo.filing_controls(decl, agent_bot(AGENT), kind="import", stage="gate")
    extra += customs_aeo.agent_reasons(aeo)
    return await decide(
        AGENT, "file_declaration", subject=("customs_declaration", decl["id"]), subject_label=lrn,
        company_id=decl.get("company_id"), confidence=conf, model=None,
        inputs={"lrn": lrn, "representation": rep, "items": [{"seq": i.get("seq"), "cn": i.get("cn_code"),
                                                               "cn_confidence": i.get("cn_confidence"),
                                                               "confirmed_by_agent": bool(i.get("cn_confirmed_by_agent"))}
                                                              for i in decl.get("items") or []],
                "totals": t, "vat_route": decl.get("vat_route"), "delegated": settings.get("customs_filing_delegated"),
                "aeo": customs_aeo.summary(aeo)},
        decision={"file": True, "gateway": (decl.get("submission") or {}).get("channel")},
        spend_cents=duty + vat, exposure_cents=duty + vat, legal_point=legal, extra_reasons=extra,
        approval_kind="customs_filing", title=(approval or {}).get("title") or f"File import declaration {lrn}",
        title_fi=f"Tuonti-ilmoituksen {lrn} jättäminen ({ref})",
        summary=(approval or {}).get("summary") or "",
        summary_fi=(f"{decl.get('company_name')}: {len(decl.get('items') or [])} nimikettä, tullausarvo "
                    f"{(t.get('customs_value_cents') or 0) / 100:,.2f} €, tulli {duty / 100:,.2f} €, ALV {vat / 100:,.2f} €, "
                    f"{'suora' if rep == 'direct' else 'epäsuora'} edustus").replace(",", " "),
        link=f"/ops/customs/{decl['id']}", amount_cents=duty, create_approval=create_approval)


async def broker_patch(decl: dict, shipment: dict, floor: float) -> tuple[dict, list[dict]]:
    """Fields the broker would fill, and per-item confirmations the agent is sure about."""
    body: dict = {}
    notes = []
    unloc, mode, _leg = _arrival(shipment)
    if not (decl.get("office") or {}).get("code") and shipment.get("office_of_presentation"):
        body["office"] = shipment["office_of_presentation"]
    elif not (decl.get("office") or {}).get("code"):
        from services import customs_offices
        try:
            candidates = await customs_offices.presentation(unloc, mode)
        except customs_offices.OfficeDataUnavailable:
            code, name = OFFICES.get((unloc, mode), DEFAULT_OFFICE)
            body["office"] = {"code": code, "name": name, "source": "offline_fallback", "verified": False}
            notes.append({"code": "COL-OFFLINE", "fi": "Offline-ehdotus: toimipaikka on tarkistettava COL-luettelosta.",
                          "en": "Offline suggestion: office requires verification against COL."})
        else:
            if len(candidates) == 1:
                selected = candidates[0]
                body["office"] = {"code": selected["code"], "name": selected["name"], "source": "EU COL",
                                  "source_url": selected["source_url"], "sha256": selected["sha256"],
                                  "fetched_at": selected["fetched_at"], "role": "ENT", "verified": True}
            else:
                notes.append({"code": "COL-SELECTION", "candidates": [r["code"] for r in candidates],
                              "fi": "Saapumispaikalle ei löytynyt yhtä yksiselitteistä COL-toimipaikkaa.",
                              "en": "No unique COL office for the arrival location and mode; review required."})
    # Only a shipment/booking can attest where these goods are presented: structured location per Tulli's MIG.
    from services.customs_import_data import location_patch
    body.update(location_patch(decl, shipment))
    tdoc = _transport_doc(shipment)
    have = (decl.get("transport") or {}).get("transport_document") or {}
    if tdoc and not _tdoc_number(decl):
        body["transport"] = {"transport_document_number": tdoc}
    if (tdoc or have.get("number")) and not have.get("type"):
        from services.customs import transport_doc_type
        main = next((lg for lg in shipment.get("legs") or [] if lg.get("transport_doc_no")), {})
        tt = transport_doc_type(main.get("mode") or mode, main)  # s11: FI415B needs the type; sea stays a person's call
        if tt:
            body.setdefault("transport", {})["transport_document_type"] = tt
    items = []
    for it in decl.get("items") or []:
        if it.get("cn_confirmed"):
            continue
        c, why = item_confidence(it)
        notes.append({"seq": it.get("seq"), "cn": it.get("cn_code"), "confidence": round(c, 3), "why": why})
        if c >= floor and it.get("cn_code"):
            items.append({"seq": it["seq"], "cn_code": it["cn_code"], "cn_confirmed": True})
    if items:
        body["items"] = items
    return body, notes


@job("customs.review_and_file", agent=True, max_attempts=6, backoff=(60, 300, 900, 1800, 3600, 7200), timeout=300,
     label_fi="Tulli-ilmoituksen tarkistus ja jättäminen", label_en="Customs review and filing")
async def review_and_file(j):
    from services import customs
    did = j["payload"]["declaration_id"]
    async with entity_lock(f"customs:{did}"):
        d = await db.raw.customs_declarations.find_one({"id": did}, NO_ID)
        if not d or d.get("demo"):
            return {"skipped": "not found or demo"}
        if d["status"] in FINAL:
            return {"skipped": d["status"]}
        s = await db.raw.shipments.find_one({"id": d.get("shipment_id")}, NO_ID) or {}
        if s.get("customs_handling") in ("broker", "customer_broker") or s.get("customs_filing_status") in ("postponed", "customer_arranged"):
            return {"skipped": "a broker handles this shipment's customs by hand"}
        settings = await get_settings()
        bot = agent_bot(AGENT)
        if d["status"] == "rejected":
            dec = await decide(AGENT, "review_rejection", subject=("customs_declaration", did), subject_label=d.get("lrn"),
                               company_id=d.get("company_id"),
                               extra_reasons=[policy_reason("tulli_rejected", "Tulli hylkäsi ilmoituksen; henkilö korjaa sen",
                                                            "Customs rejected the declaration; a person corrects it")],
                               approval_kind="document_review", title=f"Customs rejected {d.get('lrn')}",
                               title_fi=f"Tulli hylkäsi ilmoituksen {d.get('lrn')}", link=f"/ops/customs/{did}")
            return {"owner_queue": dec.approval and dec.approval["id"]}
        cust_block = customs.blocking(d, owner="customer")
        if d["status"] in ("draft", "needs_info") and cust_block:
            await customer_message(
                d["company_id"], d.get("shipment_id"),
                "Tulli-ilmoitukseen tarvitaan vielä tietoja: " + "; ".join((v.get("message_i18n") or {}).get("fi", v["message"]) for v in cust_block[:4])
                + ". Täydennä ne Huolitsijan tulli-sivulla.",
                "The customs declaration still needs information: " + "; ".join(v["message"] for v in cust_block[:4])
                + ". Please complete it on the customs page in Huolitsija.",
                subject=f"Tulli {d.get('lrn')}", key=f"customs-needs:{did}:{now_iso()[:10]}")
            raise JobDeferred(900, "waiting for the customer's data")
        body, notes = await broker_patch(d, s, float(settings["guard_confidence_floor"]))
        if not _tdoc_number(d) and "transport" not in body:
            raise JobDeferred(120, "waiting for the carrier's transport document number")
        if body:
            d = await customs.apply_patch(d, body, bot)
            conf = {n["seq"]: n for n in notes if "seq" in n}
            items = d.get("items") or []
            changed = False
            for it in items:
                n = conf.get(it.get("seq"))
                if n and it.get("cn_confirmed") and not it.get("cn_confidence"):
                    it.update(cn_confirmed_by_agent=True, cn_confidence=n["confidence"], cn_confirm_reason=n["why"])
                    changed = True
            if changed:
                await db.raw.customs_declarations.update_one({"id": did}, {"$set": {"items": items}})
                d["items"] = items
        # Drafts may originate outside document.extracted; review owns current all-party screening.
        d["screening"] = await customs.screen_seller(d)
        d = await customs.save_and_recompute(d, bot, "autopilot_screened")
        still = customs.blocking(d)
        if still:
            dec = await decide(
                AGENT, "complete_declaration", subject=("customs_declaration", did), subject_label=d.get("lrn"),
                company_id=d.get("company_id"), inputs={"blocking": still, "items": notes},
                extra_reasons=[policy_reason(f"check_{v['rule']}",
                                             (f"Nimike {v['item']}: " if v.get("item") else "") + (v.get("message_i18n") or {}).get("fi", v["message"]),
                                             (f"Item {v['item']}: " if v.get("item") else "") + v["message"]) for v in still[:6]],
                approval_kind="document_review", title=f"Customs declaration {d.get('lrn')} needs a person",
                title_fi=f"Tulli-ilmoitus {d.get('lrn')} vaatii henkilön",
                summary="; ".join(v["message"] for v in still[:5]), link=f"/ops/customs/{did}")
            return {"blocking": [v["rule"] for v in still], "approval_id": dec.approval and dec.approval["id"]}
        confirmed = bool((d.get("confirmation") or {}).get("at"))
        if d["status"] in ("draft", "needs_info") and not confirmed:
            started = parse_iso(d.get("created_at")) or now()
            wait = timedelta(hours=float(settings.get("customs_confirm_timeout_hours") or 0))
            if now() - started < wait:
                await customer_message(
                    d["company_id"], d.get("shipment_id"),
                    f"Tulli-ilmoitus {d.get('lrn')} on valmis. Tarkista tavaratiedot ja vahvista ne Huolitsijassa; jos et "
                    f"vahvista, jätämme ilmoituksen asiakirjojen tietojen perusteella.",
                    f"Customs declaration {d.get('lrn')} is ready. Please check and confirm the goods data in Huolitsija; "
                    f"otherwise we file it from the documents.", subject=f"Tulli {d.get('lrn')}", key=f"customs-confirm:{did}")
                raise JobDeferred(min(900, max(60, (started + wait - now()).total_seconds())), "waiting for the customer's confirmation")
        d = await customs.request_filing(d, bot)
        return {"status": d["status"], "approval_id": d.get("approval_id"),
                "submission": (d.get("submission") or {}).get("state")}


@job("customs.regate", agent=True, max_attempts=3, timeout=120, label_fi="Tulli-ilmoituksen hyväksyntä", label_en="Customs filing approval")
async def regate(j):
    """A filing card that waits in the owner queue is decided again when Autopilot, the delegation or limits change."""
    from services import customs
    a = await db.raw.approvals.find_one({"id": j["payload"]["approval_id"], "status": "pending"}, NO_ID)
    if not a:
        return {"skipped": "decided"}
    d = await db.raw.customs_declarations.find_one({"id": a["ref_id"]}, NO_ID)
    if not d or d.get("approval_id") != a["id"] or d["status"] != "ready_for_review":
        return {"skipped": "declaration moved on"}
    if await db.raw.shipments.find_one({"id": d.get("shipment_id"), "$or": [{"customs_handling": {"$in": ["broker", "customer_broker"]}},
            {"customs_filing_status": {"$in": ["postponed", "customer_arranged"]}}]}, {"_id": 1}):
        return {"skipped": "a broker handles this shipment's customs by hand"}
    if (a.get("data") or {}).get("fingerprint") != customs.filing_fingerprint(d):
        return {"skipped": "content changed"}
    d = await customs.save_and_recompute(d, agent_bot(AGENT), "autopilot_validation")
    if customs.blocking(d):
        return {"skipped": "blocking checks"}
    gate = await gate_filing(d, a)
    if gate.auto:
        await approve_as_agent(a, AGENT, "Autopilot: inside the guardrails, delegation recorded")
        return {"approved": True}
    return {"owner_queue": True}


@sweeper
async def sweep_customs():
    n = 0
    mk = await mode_key()
    async for d in db.raw.customs_declarations.find(
            {"status": {"$in": ["draft", "needs_info", "ready_for_review", "rejected"]}, "demo": {"$ne": True},
             "created_at": {"$gte": await agents_epoch()}},
            {"_id": 0, "id": 1, "updated_at": 1, "company_id": 1, "approval_id": 1, "status": 1}).limit(100):
        if d["status"] == "ready_for_review" and d.get("approval_id"):
            continue  # waits for its filing card (re-gated below)
        await enqueue("customs.review_and_file", {"declaration_id": d["id"]}, company_id=d.get("company_id"),
                      subject=("customs_declaration", d["id"]), key=f"customs:{d['id']}:{d.get('updated_at')}:{mk}",
                      source="sweep")
        n += 1
    if mk == "on":
        s = await get_settings()
        async for a in db.raw.approvals.find({"kind": "customs_filing", "status": "pending"}, {"_id": 0, "id": 1}).limit(50):
            await enqueue("customs.regate", {"approval_id": a["id"]}, subject=("approval", a["id"]),
                          key=f"customs-regate:{a['id']}:{s['customs_filing_delegated']}:{s['customs_delegation_scope']}:{mk}",
                          source="sweep")
            n += 1
    return n


@on("customs.released")
async def _released(p):
    # Kick the invoice agent: a delivered shipment waiting for the customs decision continues from here.
    from core.jobs import enqueue as _enq
    d = p["declaration"]
    if d.get("shipment_id"):
        await _enq("invoice.issue_on_delivery", {"shipment_id": d["shipment_id"]}, company_id=d.get("company_id"),
                   subject=("shipment", d["shipment_id"]), key=f"invoice:{d['shipment_id']}:released:{d['id']}")
