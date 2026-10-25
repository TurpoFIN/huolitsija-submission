"""The world's own model client (OpenAI Responses API). Kept apart from core.llm so the actors' thinking never shows up
as Huolitsija's AI runs; every call is recorded in world_llm_calls for the trace."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from core import config
from core.db import db
from core.util import new_id, now_iso

from . import conf

log = logging.getLogger("demo_world.llm")
_slots = asyncio.Semaphore(6)


def _parse(text: str):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


async def ask_json(actor: str, system: str, prompt: str, *, run_id: str | None = None, max_tokens: int = 2500,
                   web_search: bool = False) -> dict:
    """One persona decision as JSON. Retries transient failures; raises after the last attempt."""
    body = {"model": conf.model(), "instructions": system + "\n\nAnswer with one JSON object only.",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "max_output_tokens": max_tokens + 6000, "store": False, "reasoning": {"effort": conf.reasoning()}}
    if web_search:
        body["tools"] = [{"type": "web_search"}]
    last = None
    for delay in (0, 3, 10, 25):
        if delay:
            await asyncio.sleep(delay)
        t0 = time.monotonic()
        try:
            async with _slots:
                async with httpx.AsyncClient(timeout=180) as client:
                    r = await client.post(f"{config.OPENAI_BASE_URL}/responses", json=body,
                                          headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
            if r.status_code != 200:
                raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:300]}")
            data = r.json()
            text = "".join(c.get("text", "") for item in data.get("output") or [] if item.get("type") == "message"
                           for c in item.get("content") or [] if c.get("type") == "output_text")
            out = _parse(text)
            await db.raw.world_llm_calls.insert_one({"id": new_id(), "run_id": run_id, "actor": actor, "at": now_iso(),
                                                     "model": data.get("model") or body["model"], "prompt": prompt[:6000],
                                                     "output": out, "latency_ms": int((time.monotonic() - t0) * 1000)})
            return out
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("world persona %s call failed: %s", actor, exc)
    raise RuntimeError(f"world persona {actor} unavailable: {last}")
