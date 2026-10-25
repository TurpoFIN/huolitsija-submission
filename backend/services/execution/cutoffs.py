"""Cut-offs per carrier booking and the watch that alerts before and at each one.

Codes are DCSA's `shipmentCutOffTimes` codes (Booking 2.0): DCO documentation (SI), VCO verified gross mass,
FCO FCL cargo delivered to the terminal, LCO LCL cargo, ECP empty container pick-up, EFC earliest full container
delivery. A carrier's own cut-offs from the booking confirmation win; otherwise they are derived from the ETD of the
main leg (DCO -48 h, VCO -24 h, FCO -24 h) and marked source "derived".
"""
from __future__ import annotations

from datetime import timedelta

from core.db import db
from core.events import emit
from core.settings import get_settings
from core.util import iso, now, now_iso, parse_iso
from services.execution.runtime import job, raise_alert, resolve_alerts, schedule

LABELS = {
    "DCO": ("Asiakirjojen määräaika (SI)", "Documentation cut-off (SI)"),
    "VCO": ("VGM-määräaika", "VGM cut-off"),
    "FCO": ("Kontin toimitus terminaaliin", "FCL delivery cut-off"),
    "LCO": ("LCL-rahdin toimitus", "LCL delivery cut-off"),
    "ECP": ("Tyhjän kontin nouto", "Empty container pick-up"),
    "EFC": ("Aikaisin täyden kontin toimitus", "Earliest full container delivery"),
}
DERIVED_HOURS = {"DCO": 48, "VCO": 24, "FCO": 24}
# what completes each cut-off on the execution record
DONE_BY = {"DCO": "si_submitted_at", "VCO": "vgm_submitted_at", "FCO": "gate_in_at", "LCO": "gate_in_at"}


def build(booking_cutoffs: list[dict] | None, etd: str | None) -> list[dict]:
    out, seen = [], set()
    for c in booking_cutoffs or []:
        code = str(c.get("code") or c.get("cutOffDateTimeCode") or "").upper()
        at = c.get("at") or c.get("cutOffDateTime")
        if code in LABELS and parse_iso(at) and code not in seen:
            out.append({"code": code, "at": iso(parse_iso(at)), "source": "carrier", "alerts_sent": [], "state": "open"})
            seen.add(code)
    t = parse_iso(etd)
    if t:
        for code, h in DERIVED_HOURS.items():
            if code not in seen:
                out.append({"code": code, "at": iso(t - timedelta(hours=h)), "source": "derived", "alerts_sent": [], "state": "open"})
    for c in out:
        c["label_fi"], c["label_en"] = LABELS[c["code"]]
    return sorted(out, key=lambda c: c["at"])


def state_of(c: dict, record: dict, alert_hours: list[int]) -> str:
    field = DONE_BY.get(c["code"])
    if field and record.get(field):
        return "done"
    if c.get("state") == "done":
        return "done"
    left = (parse_iso(c["at"]) - now()).total_seconds() / 3600
    if left < 0:
        return "missed"
    return "due_soon" if left <= max(alert_hours or [24]) else "ok"


def status(record: dict, alert_hours: list[int] | None = None) -> list[dict]:
    hours = alert_hours or [48, 24, 6]
    return [{**c, "state": state_of(c, record, hours)} for c in record.get("cutoffs") or []]


async def watch_once() -> dict:
    """Alert once per threshold before each open cut-off, and once when it is missed."""
    settings = await get_settings()
    hours = sorted(int(h) for h in settings.get("execution_cutoff_alert_hours") or [48, 24, 6])
    alerts = missed = 0
    async for rec in db.execution_records.find({"state": {"$nin": ["complete", "cancelled"]}}, {"_id": 0}):
        changed = False
        for c in rec.get("cutoffs") or []:
            st = state_of(c, rec, hours)
            if st == "done":
                if c.get("state") != "done":
                    c["state"], changed = "done", True
                    await resolve_alerts(f"cutoff:{rec['id']}:{c['code']}:")
                continue
            left = (parse_iso(c["at"]) - now()).total_seconds() / 3600
            label = f"{rec.get('reference')} · {c['label_fi']}"
            if st == "missed" and c.get("state") != "missed":
                c["state"], changed = "missed", True
                missed += 1
                await raise_alert("cutoff_missed", f"cutoff:{rec['id']}:{c['code']}:missed", f"Määräaika ohitettu: {label}",
                                  f"Cut-off missed: {rec.get('reference')} · {c['label_en']}",
                                  f"{c['label_fi']} oli {c['at']}. Kuljettaja voi siirtää kontin seuraavaan lähtöön.",
                                  f"{c['label_en']} was {c['at']}. The carrier may roll the container to the next sailing.",
                                  severity="high", subject=("shipment", rec["shipment_id"]),
                                  link=f"/ops/shipments/{rec['shipment_id']}", company_id=rec.get("company_id"))
                await emit("cutoff.missed", {"record": rec, "cutoff": c}, background=True)
                continue
            if st == "missed":
                continue
            for h in hours:
                if left <= h and h not in c.get("alerts_sent", []):
                    # the most urgent threshold reached speaks for every wider one (no 24 h alert after the 6 h one)
                    c["alerts_sent"] = sorted(set(c.get("alerts_sent", [])) | {x for x in hours if x >= h})
                    c["state"], changed = "due_soon", True
                    alerts += 1
                    await raise_alert("cutoff_due", f"cutoff:{rec['id']}:{c['code']}:{h}", f"Määräaika {h} h: {label}",
                                      f"Cut-off in {h} h: {rec.get('reference')} · {c['label_en']}",
                                      f"{c['label_fi']} {c['at']} — vielä tekemättä.", f"{c['label_en']} {c['at']} — not done yet.",
                                      severity="high" if h <= 6 else "medium", subject=("shipment", rec["shipment_id"]),
                                      link=f"/ops/shipments/{rec['shipment_id']}", company_id=rec.get("company_id"))
                    await emit("cutoff.due_soon", {"record": rec, "cutoff": c, "hours": h}, background=True)
                    break
        if changed:
            await db.execution_records.update_one({"id": rec["id"]}, {"$set": {"cutoffs": rec["cutoffs"], "updated_at": now_iso()}})
    return {"alerts": alerts, "missed": missed}


@job("execution.cutoff_watch", max_attempts=3, backoff=(60, 300, 900), timeout=120, agent=False,
     label_fi="Määräaikojen valvonta", label_en="Cut-off watch")
async def cutoff_watch(j: dict) -> dict:
    return await watch_once()


schedule("execution.cutoff_watch", job="execution.cutoff_watch", every_seconds=300,
         label_fi="SI-, VGM- ja konttimääräaikojen valvonta", label_en="SI, VGM and container cut-off watch")()
