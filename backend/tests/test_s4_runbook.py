"""Owner runbook on the deployed app: internal staff read the shipped runbook documents; nobody else does."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncio
import httpx
from fastapi import FastAPI
from core.deps import current_user
from routes import runbook

SHARED = Path(__file__).resolve().parents[2] / "shared" / "s4"


def get(path, org_type="internal"):
    async def go():
        app = FastAPI()
        app.include_router(runbook.router)
        app.dependency_overrides[current_user] = lambda: {"id": "u1", "org_type": org_type, "permissions": []}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(path)
    return asyncio.run(go())


def test_list_starts_with_the_runbook_and_offers_the_pdf():
    r = get("/api/runbook")
    assert r.status_code == 200
    body = r.json()
    ids = [d["id"] for d in body["items"]]
    assert ids == ["runbook", "activation", "legal-points", "final-run", "processors", "config"]
    assert body["items"][0]["title"] == "Huolitsija operating and go-live runbook"
    assert body["pdf"] is True


def test_document_is_returned_as_markdown_and_unknown_ids_are_404():
    r = get("/api/runbook/activation")
    assert r.status_code == 200 and r.json()["markdown"].startswith("# Owner activation actions")
    assert get("/api/runbook/../../server.py").status_code == 404
    assert get("/api/runbook/nope").status_code == 404


def test_pdf_downloads():
    r = get("/api/runbook/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")


def test_customers_and_partners_cannot_read_it():
    for org in ("customer", "partner"):
        assert get("/api/runbook", org).status_code == 403
        assert get("/api/runbook/runbook", org).status_code == 403
        assert get("/api/runbook/pdf", org).status_code == 403


def test_shipped_copy_matches_shared_s4_when_present():
    # scripts/sync-runbook.sh keeps backend/data/runbook equal to shared/s4; a stale copy would put an old runbook live.
    if not SHARED.is_dir():
        return
    for name in list(runbook.DOCS.values()) + [runbook.PDF]:
        assert (runbook.DIR / name).read_bytes() == (SHARED / name).read_bytes(), name
