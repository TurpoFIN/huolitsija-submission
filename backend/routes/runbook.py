"""Owner runbook on the deployed app: the go-live runbook and its companion documents, read-only, internal staff only.

Source of truth is shared/s4 (kept by the legal/runbook lane); scripts/sync-runbook.sh copies it into backend/data/runbook so
the deployed backend carries it. GET /api/runbook lists the documents, GET /api/runbook/{id} returns one as Markdown,
GET /api/runbook/pdf downloads the printable runbook."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from core.deps import require_internal

router = APIRouter(prefix="/api/runbook", tags=["runbook"])

DIR = Path(__file__).resolve().parents[1] / "data" / "runbook"
# Reading order: the runbook first, then what the owner must do, then the reference material.
DOCS = {
    "runbook": "RUNBOOK.md",
    "activation": "OWNER-ACTIVATION.md",
    "legal-points": "LEGAL-HUMAN-POINTS.md",
    "final-run": "FINAL-RUN-PLAN.md",
    "processors": "PROCESSORS.md",
    "config": "CONFIG-INVENTORY.md",
}
PDF = "HUOLITSIJA-RUNBOOK.pdf"


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


@router.get("")
async def list_docs(user: dict = Depends(require_internal)):
    items = []
    for doc_id, name in DOCS.items():
        path = DIR / name
        if path.is_file():
            text = path.read_text("utf-8")
            items.append({"id": doc_id, "file": name, "title": _title(text, name), "bytes": len(text.encode("utf-8"))})
    return {"items": items, "pdf": (DIR / PDF).is_file(), "source": "shared/s4"}


@router.get("/pdf")
async def runbook_pdf(user: dict = Depends(require_internal)):
    path = DIR / PDF
    if not path.is_file():
        raise HTTPException(404, "Document not found")
    return FileResponse(path, media_type="application/pdf", filename=PDF)


@router.get("/{doc_id}")
async def get_doc(doc_id: str, user: dict = Depends(require_internal)):
    name = DOCS.get(doc_id)
    if not name or not (DIR / name).is_file():
        raise HTTPException(404, "Document not found")
    text = (DIR / name).read_text("utf-8")
    return {"id": doc_id, "file": name, "title": _title(text, name), "markdown": text}
