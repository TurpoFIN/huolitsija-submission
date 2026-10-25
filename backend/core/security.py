import secrets
from datetime import timedelta

import bcrypt
import jwt

from core import config
from core.util import now

COOKIE_NAME = "huo_session"
_secret: str | None = config.JWT_SECRET or None


async def load_secret(db):
    """Use JWT_SECRET from env, else one random secret persisted in MongoDB."""
    global _secret
    if _secret:
        return
    doc = await db.system.find_one({"key": "jwt_secret"})
    if not doc:
        value = secrets.token_urlsafe(48)
        await db.system.update_one({"key": "jwt_secret"}, {"$setOnInsert": {"value": value}}, upsert=True)
        doc = await db.system.find_one({"key": "jwt_secret"})
    _secret = doc["value"]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_session_token(user_id: str, session_version: int = 0) -> str:
    exp = now() + timedelta(days=config.SESSION_DAYS)
    return jwt.encode({"sub": user_id, "sv": session_version, "exp": exp}, _secret, algorithm="HS256")


def decode_session_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
