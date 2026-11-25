from collections.abc import Awaitable, Callable
import json
from typing import Any
import uuid

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.database import get_db
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.rate_limiter import enforce_rate_limit
from app.core.redis import record_redis_error
from app.core.security import decode_access_token
from app.models.base import UserRole
from app.models.user import User

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_token_payload(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Authentication required", "UNAUTHORIZED")

    payload = decode_access_token(credentials.credentials)

    jti = str(payload["jti"])
    try:
        client = redis_module.get_redis()
        if await client.exists(f"jwt:blacklist:{jti}"):
            raise UnauthorizedError("Token has been revoked", "TOKEN_REVOKED")
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()

    return payload


async def get_current_user(
    payload: dict[str, Any] = Depends(get_current_token_payload),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except ValueError as exc:
        raise UnauthorizedError("Invalid token subject", "INVALID_TOKEN") from exc

    cache_key = f"user:active:{user_id}"
    try:
        client = redis_module.get_redis()
        cached_raw = await client.get(cache_key)
        if cached_raw is not None:
            cached = json.loads(cached_raw)
            if not cached.get("is_active", False):
                raise UnauthorizedError("User account is inactive", "USER_INACTIVE")
            return User(
                id=uuid.UUID(cached["id"]),
                name=cached["name"],
                role=UserRole(cached["role"]),
                email=cached.get("email"),
                staff_id=cached.get("staff_id"),
                is_active=True,
            )
    except UnauthorizedError:
        raise
    except (RedisError, OSError, TimeoutError, ValueError, KeyError):
        await record_redis_error()

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise UnauthorizedError("User account is inactive or missing", "USER_INACTIVE")

    cache_payload = json.dumps(
        {
            "id": str(user.id),
            "name": user.name,
            "role": user.role.value,
            "email": user.email,
            "staff_id": user.staff_id,
            "is_active": user.is_active,
        }
    )
    try:
        client = redis_module.get_redis()
        await client.set(cache_key, cache_payload, ex=30)
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()

    return user


def require_roles(*roles: UserRole) -> Callable[[User], Awaitable[User]]:
    async def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise ForbiddenError("Insufficient permissions", "FORBIDDEN")
        return current_user

    return dependency


def user_rate_limit(
    scope: str,
    limit: int,
    window_seconds: int = 60,
) -> Callable[[User], Awaitable[None]]:
    async def dependency(current_user: User = Depends(get_current_user)) -> None:
        await enforce_rate_limit(
            scope=scope,
            identifier=str(current_user.id),
            limit=limit,
            window_seconds=window_seconds,
        )

    return dependency
