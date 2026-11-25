from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

REDIS_ERROR_METRIC_KEY = "metrics:redis_errors"

_redis_client: Redis | None = None
_worker_redis_error_count: int = 0


def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=1,
            retry_on_timeout=True,
        )
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def record_redis_error() -> None:
    global _worker_redis_error_count
    _worker_redis_error_count += 1
    try:
        client = get_redis()
        await client.incr(REDIS_ERROR_METRIC_KEY)
    except (RedisError, OSError, TimeoutError):
        pass


async def ping_redis() -> bool:
    try:
        client = get_redis()
        return bool(await client.ping())
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()
        return False


async def get_redis_error_metrics() -> tuple[int, str]:
    try:
        client = get_redis()
        raw_value = await client.get(REDIS_ERROR_METRIC_KEY)
        return (int(raw_value) if raw_value is not None else 0), "REDIS"
    except (RedisError, OSError, TimeoutError, ValueError):
        return _worker_redis_error_count, "WORKER_LOCAL"
