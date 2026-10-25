"""Shared plumbing for the agents: the sweep that finds work nobody has picked up (a missed event, a restart, an
Autopilot switch that changed since the last look) and enqueues one idempotent job per item."""
import logging

from core.jobs import job, schedule
from core.settings import get_settings

log = logging.getLogger("agents")
SWEEPS: list = []


def sweeper(fn):
    SWEEPS.append(fn)
    return fn


async def mode_key() -> str:
    """Part of every sweep job key: a job is re-run when Autopilot or the kill switch changes."""
    s = await get_settings()
    from core.autonomy import active_test_grants  # s6 r94: a new test-tenant grant re-runs the sweeps too
    grants = "".join(f"-t{g['company_id'][:8]}{g['expires_at']}" for g in await active_test_grants())
    return f"{'on' if s['autopilot_enabled'] else 'off'}{'-halt' if s['autopilot_halted'] else ''}{grants}"


@job("autopilot.sweep", max_attempts=2, backoff=(15,), timeout=180, label_fi="Avoimen työn haku", label_en="Find open work")
async def sweep_job(j):
    found = {}
    for fn in SWEEPS:
        try:
            found[fn.__module__.rsplit(".", 1)[-1] + "." + fn.__name__] = await fn()
        except Exception:  # noqa: BLE001
            log.exception("sweep %s failed", fn.__name__)
            found[fn.__name__] = "error"
    return found


schedule("autopilot.sweep_every_30s", job="autopilot.sweep", every_seconds=30,
         label_fi="Avoimen työn haku 30 s välein", label_en="Find open work every 30 s")()


class EntityBusy(Exception):
    pass


class entity_lock:
    """Short lease on one entity so two agent jobs (or two server instances) never work on it at once.

        async with entity_lock(f"customs:{decl_id}"):
            ...
    Raises JobDeferred when another worker holds it."""

    def __init__(self, name: str, seconds: int = 300):
        self.name, self.seconds = name, seconds
        import uuid
        self.owner = uuid.uuid4().hex

    async def __aenter__(self):
        from datetime import timedelta

        from pymongo.errors import DuplicateKeyError

        from core.db import db
        from core.jobs import JobDeferred
        from core.util import iso, now, span
        t = now()
        try:
            await db.raw.agent_locks.update_one(
                {"_id": self.name, "$or": [{"until": {"$lt": iso(t)}}, {"owner": self.owner}]},
                {"$set": {"owner": self.owner, "until": iso(t + span(self.seconds))}}, upsert=True)
        except DuplicateKeyError:
            raise JobDeferred(20, f"{self.name} is being handled by another worker")
        return self

    async def __aexit__(self, *exc):
        from core.db import db
        await db.raw.agent_locks.delete_one({"_id": self.name, "owner": self.owner})
        return False


async def customer_message(company_id: str, shipment_id: str | None, fi: str, en: str, *, subject: str | None = None,
                           key: str | None = None) -> dict | None:
    """A routine message from Huolitsija to the customer in the customer's language (system author, no AI text)."""
    from core.db import db
    from services import messaging
    from services.comms_agent import customer_locale
    if key and await db.raw.agent_messages.find_one({"_id": key}):
        return None
    lang = await customer_locale(company_id)
    body = fi if lang == "fi" else en
    msg = await messaging.post_message(company_id=company_id, shipment_id=shipment_id, subject=subject, body=body,
                                       author={"type": "system", "id": "autopilot", "name": "Huolitsija"})
    if key:
        from core.util import now_iso
        await db.raw.agent_messages.update_one({"_id": key}, {"$set": {"at": now_iso(), "message_id": msg.get("id")}},
                                               upsert=True)
    return msg


_EPOCH: dict = {}


async def agents_epoch() -> str:
    """When the agents first ran on this database. Sweeps leave older records (created before Autopilot operations
    existed) to the people who were already handling them; new events still trigger the agents for any record."""
    if "at" in _EPOCH:
        return _EPOCH["at"]
    from core.db import db
    from core.util import now_iso
    await db.raw.system.update_one({"_id": "agents_epoch"}, {"$setOnInsert": {"key": "agents_epoch", "at": now_iso()}},
                                   upsert=True)
    doc = await db.raw.system.find_one({"_id": "agents_epoch"})
    _EPOCH["at"] = doc["at"]
    return doc["at"]
