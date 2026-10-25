"""AI model access for the agents (quoting, document parsing, HS classification, exceptions, comms).

OpenAI Responses API only, configured by OPENAI_API_KEY and LLM_MODEL.
Every call is logged to ai_runs. An unavailable model requires human review, never invented output.

    data = await llm_json("hs_classifier", system, prompt, ref={"type": "declaration", "id": d_id})
    data = await llm_json("doc_parser", system, prompt, files=[("application/pdf", pdf_bytes, "invoice.pdf")])
"""
import asyncio
import base64
import json
import logging
import os
import re
import time

from core import config
from core.db import db
from core.util import new_id, now_iso

log = logging.getLogger("llm")


class LLMUnavailable(Exception):
    pass


def llm_available() -> bool:
    return config.LLM_PROVIDER == "openai" and bool(config.OPENAI_API_KEY and config.LLM_MODEL)


def provider_name() -> str:
    return "openai" if llm_available() else "none"


def _parse_json(text: str):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("Model did not return JSON")


async def _call_openai(system: str, prompt: str, files, model: str | None, max_tokens: int) -> tuple[str, str]:
    """OpenAI Responses API over plain HTTPS (no extra SDK). Images and PDFs go in as data URLs."""
    import httpx

    mdl = model or config.LLM_MODEL
    content = [{"type": "input_text", "text": prompt}]
    for mime, data, name in files or []:
        url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        if mime.startswith("image/"):
            content.append({"type": "input_image", "image_url": url})
        else:
            content.append({"type": "input_file", "filename": name or "document.pdf", "file_data": url})
    body = {"model": mdl, "instructions": system, "input": [{"role": "user", "content": content}],
            # reasoning models spend output tokens on thinking first; leave room for the answer
            "max_output_tokens": max_tokens + 8000, "store": False}
    if config.LLM_REASONING_EFFORT:
        body["reasoning"] = {"effort": config.LLM_REASONING_EFFORT}
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(f"{config.OPENAI_BASE_URL}/responses", json=body,
                              headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:500]}")
    data = r.json()
    text = "".join(c.get("text", "") for item in data.get("output") or [] if item.get("type") == "message"
                   for c in item.get("content") or [] if c.get("type") == "output_text")
    if not text and data.get("status") == "incomplete":
        raise RuntimeError(f"OpenAI response incomplete: {(data.get('incomplete_details') or {}).get('reason')}")
    return text, f"openai/{data.get('model') or mdl}"


_RETRY_DELAYS_S = (2, 6, 15)
# Shared account concurrency limits may require a longer retry window.
_BUSY_DELAYS_S = (5, 10, 20, 30, 30, 30)
_TRANSIENT = re.compile(r"429|rate.?limit|concurrent|overloaded|529|503|timed? ?out", re.I)
_BUSY = re.compile(r"concurrent_request_limit|concurrent request", re.I)
_slots: tuple | None = None  # (event loop, semaphore)


def _llm_slots() -> asyncio.Semaphore:
    """Caps parallel model calls in this process; LLM_MAX_CONCURRENCY overrides the default."""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        n = int(os.environ.get("LLM_MAX_CONCURRENCY") or 4)
        _slots = (loop, asyncio.Semaphore(max(1, n)))
    return _slots[1]


async def llm_text(agent: str, system: str, prompt: str, *, files=None, model: str | None = None,
                   max_tokens: int = 2000, ref: dict | None = None, company_id: str | None = None) -> str:
    if not llm_available():
        raise LLMUnavailable("No AI provider configured (set LLM_PROVIDER=openai, OPENAI_API_KEY and LLM_MODEL)")
    run = {"id": new_id(), "agent": agent, "created_at": now_iso(), "ref": ref or {}, "company_id": company_id,
           "provider": provider_name(), "prompt_preview": prompt[:4000], "files": [n for _m, _d, n in files or []],
           "status": "running"}
    started = time.monotonic()
    try:
        async with _llm_slots():
            text, used = await _call_with_retries(run, system, prompt, files, model, max_tokens)
        run.update(status="ok", model=used, output_preview=text[:8000])
        return text
    except Exception as exc:  # noqa: BLE001
        run.update(status="error", error=str(exc)[:2000])
        raise
    finally:
        run["latency_ms"] = int((time.monotonic() - started) * 1000)
        await db.ai_runs.insert_one(dict(run))


async def _call_with_retries(run: dict, system, prompt, files, model, max_tokens) -> tuple[str, str]:
    attempt = 0
    while True:
        try:
            return await _call_openai(system, prompt, files, model, max_tokens)
        except Exception as exc:  # noqa: BLE001
            # The key is shared with other processes (preview and live): a 429 there is worth a wait.
            delays = _BUSY_DELAYS_S if _BUSY.search(str(exc)) else _RETRY_DELAYS_S
            if attempt >= len(delays) or not _TRANSIENT.search(str(exc)):
                raise
            run["retries"] = attempt + 1
            await asyncio.sleep(delays[attempt])
            attempt += 1


async def llm_json(agent: str, system: str, prompt: str, **kwargs):
    text = await llm_text(agent, system + "\n\nAnswer with one JSON value only. No prose, no code fences.",
                          prompt, **kwargs)
    return _parse_json(text)
