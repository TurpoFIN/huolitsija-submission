"""File storage in MongoDB GridFS (persists with the database on Emergent)."""
from bson import ObjectId

from core.db import fs


async def put_file(data: bytes, filename: str, content_type: str, meta: dict | None = None) -> str:
    file_id = await fs.upload_from_stream(filename, data, metadata={"content_type": content_type, **(meta or {})})
    return str(file_id)


async def get_file(file_id: str) -> tuple[bytes, str, str]:
    stream = await fs.open_download_stream(ObjectId(file_id))
    data = await stream.read()
    meta = stream.metadata or {}
    return data, stream.filename, meta.get("content_type", "application/octet-stream")


async def delete_file(file_id: str):
    try:
        await fs.delete(ObjectId(file_id))
    except Exception:  # noqa: BLE001
        pass
