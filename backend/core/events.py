"""In-process domain event bus.

    from core.events import on, emit

    @on("shipment.delivered")
    async def make_invoice(payload): ...

    await emit("shipment.delivered", {"shipment": s, "actor": user})

Handlers run sequentially after the emitting request has done its own write; a failing
handler is logged and recorded in the `event_failures` collection, never raised to the caller.

Event names used across the app (payload keys in brackets):
  shipment.created [shipment, actor]
  shipment.status_changed [shipment, old_status, new_status, actor]
  shipment.delivered [shipment, actor]
  quote.created [quote, actor]          quote.accepted [quote, option, actor]
  document.uploaded [document, actor]   document.extracted [document]
  customs.approved [declaration, actor] customs.released [declaration]
  invoice.created [invoice]             invoice.paid [invoice]
  approval.decided [approval, actor]
  company.kyc_changed [company, actor]
  exception.opened [exception]          message.posted [thread, message]
"""
import asyncio
import logging
import traceback
from collections import defaultdict
from typing import Awaitable, Callable

log = logging.getLogger("events")
_handlers: dict[str, list[Callable[[dict], Awaitable[None]]]] = defaultdict(list)


def on(event: str):
    def deco(fn):
        _handlers[event].append(fn)
        return fn

    return deco


async def emit(event: str, payload: dict, background: bool = False):
    async def run():
        from core.db import db
        from core.util import now_iso

        for fn in list(_handlers.get(event, [])):
            try:
                await fn(payload)
            except Exception as exc:  # noqa: BLE001
                log.exception("event handler %s for %s failed", fn.__name__, event)
                try:
                    await db.event_failures.insert_one(
                        {"event": event, "handler": fn.__name__, "error": str(exc),
                         "trace": traceback.format_exc()[-4000:], "at": now_iso()}
                    )
                except Exception:  # noqa: BLE001
                    pass

    if background:
        asyncio.create_task(run())
    else:
        await run()
