"""Evidence-backed triggers; resolution remains s2's audited exception agent."""
from datetime import timedelta
from core.db import db
from core.util import now, parse_iso
from services.exceptions_agent import create_exception


def findings(s):
    at = now()
    hits = []
    pred = s.get("eta_prediction") or {}
    for leg in s.get("legs") or []:
        if leg.get("ata"):
            continue
        evidence = {"leg_seq": leg["seq"]}
        # A moved confirmed ETD, not merely a passed clock, proves a rollover.
        original, latest = parse_iso(leg.get("original_etd")), parse_iso(leg.get("etd"))
        if original and latest and latest - original >= timedelta(days=3) and leg.get("schedule_source") == "carrier_api":
            hits.append(("R-ROLLOVER", "capacity", "Carrier moved departure by at least three days",
                         "Rahdinkuljettaja siirsi lähtöä vähintään kolme päivää", {**evidence, "original_etd": leg["original_etd"], "etd": leg["etd"]}))
        for cutoff in leg.get("cutoffs") or []:
            due = parse_iso(cutoff.get("at"))
            # Absence of receipt is an evidence gap, not proof cargo missed the vessel.
            if due and due < at and cutoff.get("status") == "missed" and cutoff.get("source") in ("carrier_api", "partner"):
                hits.append(("R-CUTOFF", "capacity", "Carrier confirmed a missed cut-off", "Rahdinkuljettaja vahvisti määräajan ylittymisen", {**evidence, "cutoff": cutoff}))
        if pred.get("state") == "predicted" and pred.get("leg_seq") == leg["seq"]:
            planned, estimate = parse_iso(leg.get("eta")), parse_iso(pred.get("eta"))
            if planned and estimate and estimate - planned > timedelta(hours=24):
                hits.append(("R-ETA-PRED", "delay", "Observed movement predicts arrival over 24 hours late",
                             "Havaittu liike ennakoi yli vuorokauden viivettä", {**evidence, "planned_eta": leg["eta"], "prediction": pred}))
            congestion = (pred.get("inputs") or {}).get("port_delay") or {}
            if congestion.get("state") == "observed" and (congestion.get("arrival_delay_h_median") or 0) >= 24:
                hits.append(("R-CONGESTION", "delay", "Recent port calls show at least 24 hours of arrival delay",
                             "Viimeaikaiset satamakäynnit osoittavat vähintään vuorokauden viiveen", {**evidence, "port_delay": congestion}))
    for hold in s.get("holds") or []:
        if hold.get("type") == "customs" and hold.get("source") in ("carrier_api", "customs", "tulli"):
            hits.append(("R-CUSTOMS-FEED", "customs_hold", "Customs hold reported", "Tullin pysäytys ilmoitettu", {"hold": hold}))
    return hits


async def scan_shipment(s):
    out = []
    for rule, kind, en, fi, evidence in findings(s):
        ex, _ = await create_exception(shipment_id=s["id"], type=kind, severity="high", title=en,
                                      title_i18n={"fi": fi, "en": en}, description=en,
                                      description_i18n={"fi": fi, "en": en}, detected_by="rule",
                                      rule=rule, rule_key=f"{s['id']}:{rule}:{evidence.get('leg_seq', 0)}",
                                      evidence=evidence, assess=False)
        out.append(ex)
    return out
