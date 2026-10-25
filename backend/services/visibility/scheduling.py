from core.db import db
from core.jobs import job, schedule, enqueue
from core.util import NO_ID, now_iso
from services.visibility import feeds  # register the leased AIS job
from services.visibility.eta import predict_shipment
from services.visibility.detect import scan_shipment


@job("visibility.refresh", timeout=300, label_fi="Seurannan ennusteet", label_en="Visibility forecasts")
@schedule("visibility.refresh", job="visibility.refresh", every_seconds=300)
async def refresh(j):
    n = 0
    async for s in db.shipments.find({"status": {"$in": ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "exception"]}}, NO_ID):
        # Separate jobs prevent one slow feed from starving the entire fleet.
        await enqueue("visibility.shipment", {"shipment_id": s["id"]}, key=f"visibility:{s['id']}:{j['id']}")
        n += 1
    return {"queued": n}


@job("visibility.shipment", timeout=120, label_fi="Lähetyksen ennuste", label_en="Shipment forecast")
async def shipment(j):
    s = await db.shipments.find_one({"id": j["payload"]["shipment_id"]}, NO_ID)
    if not s or s.get("status") in ("delivered", "cancelled"):
        return
    s["eta_prediction"] = await predict_shipment(s)
    await enqueue("visibility.detect", {"shipment_id": s["id"]}, key=f"visibility-detect:{s['id']}:{j['id']}")
    return s["eta_prediction"]


@job("visibility.detect", agent=True, label_fi="Seurannan poikkeamat", label_en="Visibility exceptions")
async def detect(j):
    s = await db.shipments.find_one({"id": j["payload"]["shipment_id"]}, NO_ID)
    return {"exceptions": len(await scan_shipment(s))} if s else {"missing": True}


@job("visibility.carriers", timeout=300, label_fi="Rahdinkuljettajien seuranta", label_en="Carrier tracking")
@schedule("visibility.carriers", job="visibility.carriers", every_seconds=600)
async def carriers(j):
    from services.tracking import poll_all
    return {"events": await poll_all()}
