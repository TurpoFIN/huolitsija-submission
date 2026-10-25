"""A cold any-city index must not freeze the event loop (live: 19 s quotes, health checks timing out, edge 520s).

The real data files and a real MongoDB (own database, dropped at the end); nothing outside is called because the
cities resolve offline.
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

os.environ["DB_NAME"] = f"huo_test_s1_warm_{uuid.uuid4().hex[:8]}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import client  # noqa: E402
from services import gazetteer, pricing  # noqa: E402


def test_cold_index_builds_off_the_event_loop():
    async def go():
        client._io_loop = asyncio.get_running_loop()
        from services import geo
        for f in (geo._dataset, gazetteer._places, gazetteer._countries):
            f.cache_clear()
        getattr(gazetteer, "_warm", {}).clear()
        assert gazetteer._places.cache_info().currsize == 0
        worst = [0.0]

        async def watch():
            while True:
                t = time.perf_counter()
                await asyncio.sleep(0.02)
                worst[0] = max(worst[0], time.perf_counter() - t - 0.02)

        w = asyncio.create_task(watch())
        t0 = time.perf_counter()
        a, b = await asyncio.gather(pricing.resolve_location("Ouagadougou"), pricing.resolve_location("Kuopio"))
        took = time.perf_counter() - t0
        w.cancel()
        await client.drop_database(os.environ["DB_NAME"])
        return a, b, took, worst[0]

    a, b, took, worst = asyncio.run(go())
    assert a["country"] == "BF" and b["country"] == "FI"
    assert gazetteer._places.cache_info().currsize == 1
    assert took > 0.5, "the index was already warm; the test proves nothing"
    assert worst < 0.5, f"event loop blocked for {worst:.2f} s while the index was built ({took:.2f} s total)"
