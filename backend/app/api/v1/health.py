import time
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis_error_metrics, ping_redis

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"] = Field(
        description="Overall service health state"
    )
    db_latency_ms: float | None = Field(
        description="PostgreSQL SELECT 1 round-trip latency in milliseconds, or null when unreachable"
    )
    redis_status: Literal["up", "down"] = Field(
        description="Redis PING connectivity status"
    )
    redis_error_count: int = Field(
        description="Cumulative Redis error count from Redis or worker-local fallback"
    )
    redis_error_count_source: Literal["REDIS", "WORKER_LOCAL"] = Field(
        description="Origin of redis_error_count"
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Check PostgreSQL and Redis service health",
)
async def health_check(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    db_up = False
    db_latency_ms: float | None = None
    start = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        db_latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        db_up = True
    except Exception:
        db_up = False
        db_latency_ms = None

    redis_up = await ping_redis()
    redis_error_count, redis_error_count_source = await get_redis_error_metrics()

    if not db_up:
        overall_status: Literal["healthy", "degraded", "unhealthy"] = "unhealthy"
        status_code = 503
    elif not redis_up:
        overall_status = "degraded"
        status_code = 200
    else:
        overall_status = "healthy"
        status_code = 200

    payload = HealthResponse(
        status=overall_status,
        db_latency_ms=db_latency_ms,
        redis_status="up" if redis_up else "down",
        redis_error_count=redis_error_count,
        redis_error_count_source=redis_error_count_source,
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())
