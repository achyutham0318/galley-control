import asyncio
from datetime import timedelta
from typing import Any
import uuid

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.clock import now
from app.core.config import settings
from app.core.errors import UnauthorizedError
from app.models.base import UserRole

DUMMY_BCRYPT_HASH: str = bcrypt.hashpw(
    b"galley-dummy-constant-time-secret",
    bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS),
).decode("utf-8")


def _hash_sync(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def _verify_sync(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


async def hash_secret(plain: str) -> str:
    return await asyncio.to_thread(_hash_sync, plain)


async def verify_secret(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify_sync, plain, hashed)


def create_access_token(
    user_id: uuid.UUID,
    role: UserRole,
    restaurant_id: uuid.UUID | None = None,
    restaurant_name: str | None = None,
) -> str:
    issued_at = now()
    expires_at = issued_at + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role.value,
        "jti": str(uuid.uuid4()),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if restaurant_id is not None:
        payload["restaurant_id"] = str(restaurant_id)
    if restaurant_name is not None:
        payload["restaurant_name"] = restaurant_name
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={
                "verify_exp": False,
                "require": ["sub", "role", "jti", "iat", "exp"],
            },
        )
    except InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or malformed token", "INVALID_TOKEN") from exc

    if int(payload["exp"]) <= int(now().timestamp()):
        raise UnauthorizedError("Token has expired", "TOKEN_EXPIRED")

    return payload
