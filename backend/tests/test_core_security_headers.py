"""Every API response carries HSTS, nosniff, referrer and framing policy (review R2). Offline: no startup, no DB."""
import asyncio

import httpx

import server


def _get(path):
    async def run():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.get(path)

    return asyncio.run(run())


def test_security_headers_on_api_response():
    r = _get("/api/version")
    assert r.status_code == 200
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_security_headers_on_errors_too():
    r = _get("/api/no-such-route")
    assert r.status_code == 404
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
