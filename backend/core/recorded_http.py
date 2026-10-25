"""Recorded real responses from public registers (PRH/YTJ open data, PRH XBRL, EU VIES, EU EORI) for CI.

Production never sets these, so every call goes to the live register. The CI backend (scripts/run-ci-backend.sh) sets
REGISTER_RECORDINGS=<dir> so the suites do not depend on a register being reachable at test time:

    REGISTER_RECORDINGS=<dir>     answer a call from <dir>/index.json when that exact request was recorded; other
                                  requests still go to the live register (logged as "live").
    REGISTER_RECORD=1             (with REGISTER_RECORDINGS) call the live register and store its answer in <dir>.
    REGISTER_RECORDINGS_STRICT=1  (with REGISTER_RECORDINGS) an unrecorded request raises httpx.ConnectError instead.

Recordings are real answers captured with REGISTER_RECORD=1 (status, content type, body, recorded_at); they are never
hand-written. Re-record with: REGISTER_RECORDINGS=backend/tests/fixtures/registers REGISTER_RECORD=1 on the CI backend.

    r = await recorded_http.request("GET", url, headers=UA, timeout=20)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("recorded_http")
_lock = threading.Lock()


def _dir() -> Path | None:
    d = (os.environ.get("REGISTER_RECORDINGS") or "").strip()
    return Path(d) if d else None


def _key(method: str, url: str, content: bytes | None) -> str:
    k = f"{method.upper()} {url}"
    if content:
        k += " #" + hashlib.sha256(content).hexdigest()[:16]
    return k


def _index(d: Path) -> dict:
    try:
        return json.loads((d / "index.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _replay(d: Path, key: str, url: str) -> httpx.Response | None:
    e = _index(d).get(key)
    if not e:
        return None
    body = (d / e["file"]).read_bytes()
    return httpx.Response(e["status"], headers={"content-type": e.get("content_type") or "application/octet-stream",
                                                "x-recorded-at": e.get("recorded_at") or ""},
                          content=body, request=httpx.Request("GET", url))


def _store(d: Path, key: str, r: httpx.Response):
    ctype = r.headers.get("content-type") or ""
    ext = "json" if "json" in ctype else "xml" if "xml" in ctype else "txt"
    name = hashlib.sha256(key.encode()).hexdigest()[:20] + "." + ext
    with _lock:
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(r.content)
        idx = _index(d)
        idx[key] = {"status": r.status_code, "content_type": ctype, "file": name,
                    "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        (d / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


async def request(method: str, url: str, *, headers: dict | None = None, content: bytes | None = None,
                  timeout: float = 20, follow_redirects: bool = False) -> httpx.Response:
    """httpx request with optional recording / replay (see module doc). Raises httpx.HTTPError like httpx does."""
    from services.demo_world import boundary
    captured = await boundary.registry_http(method, url, content=content)
    if captured is not None:
        return captured
    d = _dir()
    key = _key(method, url, content)
    record = d is not None and os.environ.get("REGISTER_RECORD") == "1"
    if d is not None and not record:
        hit = _replay(d, key, url)
        if hit is not None:
            return hit
        if os.environ.get("REGISTER_RECORDINGS_STRICT") == "1":
            raise httpx.ConnectError(f"no recording for {key}")
        log.info("register call not recorded, going live: %s", key)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=follow_redirects) as c:
        r = await c.request(method, url, content=content)
    if record and r.status_code < 500:
        _store(d, key, r)
    return r
