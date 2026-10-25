"""core.llm retries transient provider errors (429 / overloaded) and fails fast on others. Offline."""
import asyncio

from core import config, llm

BUSY_DELAYS_S = llm._BUSY_DELAYS_S  # the real schedule, before tests patch it to zeros


class _Runs:
    last = None

    async def insert_one(self, doc):
        _Runs.last = doc


def _setup(monkeypatch, call):
    monkeypatch.setattr(llm, "db", type("D", (), {"ai_runs": _Runs()})())
    monkeypatch.setattr(llm, "_RETRY_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(llm, "_BUSY_DELAYS_S", (0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_call_openai", call)


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    calls = []

    async def flaky(*_a):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("litellm.RateLimitError: Error code: 429")
        return '{"ok": true}', "openai/gpt-6.1-sol"

    _setup(monkeypatch, flaky)
    assert asyncio.run(llm.llm_json("t", "s", "p")) == {"ok": True}
    assert len(calls) == 3 and _Runs.last["retries"] == 2 and _Runs.last["status"] == "ok"


def test_other_errors_fail_without_retry(monkeypatch):
    calls = []

    async def bad(*_a):
        calls.append(1)
        raise RuntimeError("invalid request: bad schema")

    _setup(monkeypatch, bad)
    try:
        asyncio.run(llm.llm_text("t", "s", "p"))
        raise AssertionError("expected an error")
    except RuntimeError as exc:
        assert "bad schema" in str(exc)
    assert len(calls) == 1 and _Runs.last["status"] == "error"


def test_calls_respect_configured_concurrency(monkeypatch):
    active, peak = [0], [0]

    async def slow(*_a):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.05)
        active[0] -= 1
        return "ok", "openai/gpt-6.1-sol"

    _setup(monkeypatch, slow)
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "1")

    async def burst():
        return await asyncio.gather(*(llm.llm_text("t", "s", "p") for _ in range(4)))

    assert asyncio.run(burst()) == ["ok"] * 4
    assert peak[0] == 1


def test_busy_shared_key_waits_longer_than_other_rate_limits(monkeypatch):
    """Live 41a2a56, 12:51:05: HS classification gave up after 3 retries on "concurrent_request_limit" while another
    process held the key's single slot, and the customs draft fell back to retrieval only."""
    calls = []

    async def busy(*_a):
        calls.append(1)
        if len(calls) < 6:
            raise RuntimeError("litellm.RateLimitError: Error code: 429 - {'error': 'concurrent_request_limit'}")
        return '{"ok": true}', "openai/gpt-6.1-sol"

    _setup(monkeypatch, busy)
    assert asyncio.run(llm.llm_json("t", "s", "p")) == {"ok": True}
    assert len(calls) == 6 and _Runs.last["retries"] == 5 and _Runs.last["status"] == "ok"
    assert sum(BUSY_DELAYS_S) >= 120  # outlasts two typical 60 s calls by another process

    calls.clear()

    async def limited(*_a):
        calls.append(1)
        raise RuntimeError("litellm.RateLimitError: Error code: 429 - tokens per minute")

    _setup(monkeypatch, limited)
    try:
        asyncio.run(llm.llm_text("t", "s", "p"))
        raise AssertionError("expected an error")
    except RuntimeError:
        pass
    assert len(calls) == 4 and _Runs.last["status"] == "error"
