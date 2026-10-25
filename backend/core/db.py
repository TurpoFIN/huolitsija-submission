from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket

from core import config
from core.tenant_visibility import VisibleDatabase

client = AsyncIOMotorClient(config.MONGO_URL, tz_aware=True)
db = VisibleDatabase(client[config.DB_NAME])
fs = AsyncIOMotorGridFSBucket(db.raw, bucket_name="files")

# Index definitions per collection. Slices add their own with register_indexes().
_INDEXES: list[tuple[str, list, dict]] = []


def register_index(collection: str, keys, **kwargs):
    _INDEXES.append((collection, keys, kwargs))


async def ensure_indexes():
    for coll, keys, kwargs in _INDEXES:
        try:
            await db[coll].create_index(keys, **kwargs)
        except Exception as exc:  # index conflicts must not stop the app
            import logging

            logging.getLogger("db").warning("index %s %s failed: %s", coll, keys, exc)


register_index("users", "email", unique=True)
register_index("users", "id", unique=True)
register_index("users", "company_id")
register_index("companies", "id", unique=True)
register_index("companies", "business_id")
register_index("audit_log", [("entity", 1), ("entity_id", 1), ("at", -1)])
register_index("approvals", [("status", 1), ("created_at", -1)])
register_index("approvals", "id", unique=True)
register_index("ai_runs", [("created_at", -1)])
register_index("notifications", [("user_id", 1), ("created_at", -1)])
register_index("counters", "key", unique=True)
register_index("email_outbox", [("created_at", -1)])
register_index("tokens", "token", unique=True)
