from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from redis.exceptions import RedisError

from app.core import redis as redis_module
from app.core.clock import now
from app.core.errors import RateLimitError
from app.core.redis import record_redis_error


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def enforce_rate_limit(
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int = 60,
) -> None:
    current_unix = int(now().timestamp())
    bucket = current_unix // window_seconds
    redis_key = f"rl:{scope}:{identifier}:{bucket}"
    try:
        client = redis_module.get_redis()
        count = await client.incr(redis_key)
        if count == 1:
            await client.expire(redis_key, window_seconds)
        if count > limit:
            retry_after = max(1, window_seconds - (current_unix % window_seconds))
            raise RateLimitError(
                message="Rate limit exceeded",
                code="RATE_LIMITED",
                headers={"Retry-After": str(retry_after)},
            )
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()


def ip_rate_limit(
    scope: str,
    limit: int,
    window_seconds: int = 60,
) -> Callable[[Request], Awaitable[None]]:
    async def dependency(request: Request) -> None:
        client_ip = get_client_ip(request)
        await enforce_rate_limit(
            scope=scope,
            identifier=client_ip,
            limit=limit,
            window_seconds=window_seconds,
        )

    return dependency


async def staff_pin_login_rate_limit(request: Request) -> None:
    client_ip = get_client_ip(request)
    identifier = "unknown"
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_identifier = body.get("staff_id") or body.get("email")
            if raw_identifier is not None:
                identifier = str(raw_identifier).strip().lower() or "unknown"
    except Exception:
        identifier = "unknown"

    await enforce_rate_limit(
        scope="staff_pin_login",
        identifier=f"{client_ip}:{identifier}",
        limit=10,
        window_seconds=60,
    )
