"""Startup hooks and background loops registered by slices.

    @on_startup(order=50)
    async def seed(): ...

    @background_loop(seconds=30)
    async def tick(): ...

Startup hooks run in one process at a time. A deploy can start two server processes against the same database
(2026-10-24: both ran the r3 one-time repairs, so every withdrawn quote got two approval cards), and the hooks' "run
once" markers are check-then-insert. So run_startup first takes a lock document in db.system; a second process waits
for it and then finds the markers already written.
"""
import asyncio
import logging
import time
import uuid

from pymongo.errors import DuplicateKeyError

from core.db import db

log = logging.getLogger("lifecycle")
STARTUP_LOCK = "startup-lock"
LOCK_TTL = 120  # seconds; renewed while the hooks run, so a crashed process frees it within two minutes
_startup: list[tuple[int, object]] = []
_loops: list[tuple[float, object]] = []
_tasks: list[asyncio.Task] = []


def on_startup(order: int = 100):
    def deco(fn):
        _startup.append((order, fn))
        return fn

    return deco


def background_loop(seconds: float):
    def deco(fn):
        _loops.append((seconds, fn))
        return fn

    return deco


async def acquire_startup_lock(owner: str, poll: float = 1.0) -> bool:
    """Wait until this process holds the startup lock. False when the database can't be reached (hooks run anyway,
    as before, and log their own errors)."""
    waited = 0.0
    while True:
        now = time.time()
        try:
            # Matches a free (expired) or own lock; otherwise the upsert's insert hits the same _id and raises.
            await db.system.update_one({"_id": STARTUP_LOCK, "$or": [{"expires": {"$lt": now}}, {"owner": owner}]},
                                       {"$set": {"key": STARTUP_LOCK, "owner": owner, "expires": now + LOCK_TTL}}, upsert=True)
            return True
        except DuplicateKeyError:
            if waited % 30 < poll:
                log.info("startup hooks: waiting for another process to finish them (%.0f s)", waited)
            await asyncio.sleep(poll)
            waited += poll
        except Exception:  # noqa: BLE001
            log.exception("startup lock unavailable; running the hooks without it")
            return False


async def release_startup_lock(owner: str):
    try:
        await db.system.delete_one({"_id": STARTUP_LOCK, "owner": owner})
    except Exception:  # noqa: BLE001
        log.exception("startup lock release failed (it expires by itself)")


async def run_startup():
    owner = uuid.uuid4().hex
    locked = await acquire_startup_lock(owner)

    async def renew():
        while True:
            await asyncio.sleep(LOCK_TTL / 4)
            try:
                await db.system.update_one({"_id": STARTUP_LOCK, "owner": owner}, {"$set": {"expires": time.time() + LOCK_TTL}})
            except Exception:  # noqa: BLE001
                log.exception("startup lock renewal failed")

    renewer = asyncio.create_task(renew()) if locked else None
    try:
        for _order, fn in sorted(_startup, key=lambda x: x[0]):
            try:
                await fn()
            except Exception:  # noqa: BLE001
                log.exception("startup hook %s failed", getattr(fn, "__name__", fn))
    finally:
        if renewer:
            renewer.cancel()
            await release_startup_lock(owner)


def start_loops():
    async def runner(seconds, fn):
        await asyncio.sleep(3)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("loop %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(seconds)

    for seconds, fn in _loops:
        _tasks.append(asyncio.create_task(runner(seconds, fn)))


async def stop_loops():
    for t in _tasks:
        t.cancel()
