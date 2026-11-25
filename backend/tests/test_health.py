import uuid

import httpx
import pytest
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.config import Settings
from app.core.database import get_db
from app.core.errors import NotFoundError
from app.main import app
from app.models.audit import AuditLog
from app.models.base import AuditEvent, AuditTier
from app.models.menu import MenuItem


async def test_alembic_initial_migration_creates_schema(db_session: AsyncSession) -> None:
    expected_tables = {
        "users",
        "menu_items",
        "orders",
        "order_items",
        "refunds",
        "audit_log",
        "webhook_events",
    }
    if db_session.bind is not None and db_session.bind.dialect.name == "sqlite":
        result = await db_session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
        tables = {row[0] for row in result.fetchall()}
        assert expected_tables.issubset(tables)
        return

    result = await db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    )
    tables = {row[0] for row in result.fetchall()}
    assert (expected_tables | {"alembic_version"}).issubset(tables)

    seq_result = await db_session.execute(
        text(
            "SELECT sequence_name FROM information_schema.sequences "
            "WHERE sequence_schema = 'public'"
        )
    )
    sequences = {row[0] for row in seq_result.fetchall()}
    assert "order_number_seq" in sequences


async def test_health_healthy(async_client: httpx.AsyncClient) -> None:
    response = await async_client.get("/api/v1/health")
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers
    uuid.UUID(response.headers["X-Request-ID"])

    body = response.json()
    assert body["status"] == "healthy"
    assert isinstance(body["db_latency_ms"], float)
    assert body["db_latency_ms"] >= 0.0
    assert body["redis_status"] == "up"
    assert isinstance(body["redis_error_count"], int)
    assert body["redis_error_count_source"] == "REDIS"


async def test_health_redis_down_degraded(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreachable_redis = Redis.from_url(
        "redis://127.0.0.1:6399/0",
        decode_responses=True,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    monkeypatch.setattr(redis_module, "get_redis", lambda: unreachable_redis)
    monkeypatch.setattr(redis_module, "_worker_redis_error_count", 0)

    try:
        response = await async_client.get("/api/v1/health")
    finally:
        await unreachable_redis.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["redis_status"] == "down"
    assert body["redis_error_count"] >= 1
    assert body["redis_error_count_source"] == "WORKER_LOCAL"
    assert isinstance(body["db_latency_ms"], float)


async def test_health_db_down_unhealthy(async_client: httpx.AsyncClient) -> None:
    class BrokenSession:
        async def execute(self, *args: object, **kwargs: object) -> None:
            raise ConnectionRefusedError("PostgreSQL unreachable")

    async def override_get_db():
        yield BrokenSession()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = await async_client.get("/api/v1/health")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["db_latency_ms"] is None
    assert "X-Request-ID" in response.headers


async def test_audit_log_append_only_trigger(db_session: AsyncSession) -> None:
    row_id = uuid.uuid4()
    db_session.add(
        AuditLog(
            id=row_id,
            order_id=None,
            actor_id=None,
            tier=AuditTier.TIER_1,
            event_type=AuditEvent.REFUND_EVAL,
            input_state={"test": True},
            risk_score=0.1,
            decision="AUTO_APPROVE",
            reason_code="TEST_INSERT",
            human_message="Test append only",
            latency_ms=1.5,
        )
    )
    await db_session.flush()

    param_id = (
        row_id.hex
        if db_session.bind is not None and db_session.bind.dialect.name == "sqlite"
        else row_id
    )

    with pytest.raises(DBAPIError) as update_exc:
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE audit_log SET decision = 'TAMPERED' WHERE id = :id"),
                {"id": param_id},
            )
    assert "append-only" in str(update_exc.value).lower()

    with pytest.raises(DBAPIError) as delete_exc:
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM audit_log WHERE id = :id"),
                {"id": param_id},
            )
    assert "append-only" in str(delete_exc.value).lower()


async def test_menu_item_check_constraints(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                MenuItem(
                    name="Negative Stock Item",
                    description="Invalid",
                    price_paise=5000,
                    stock_count=-1,
                    estimated_prep_minutes=5,
                )
            )
            await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                MenuItem(
                    name="Zero Price Item",
                    description="Invalid",
                    price_paise=0,
                    stock_count=10,
                    estimated_prep_minutes=5,
                )
            )
            await db_session.flush()


async def test_error_envelope_and_request_id(async_client: httpx.AsyncClient) -> None:
    not_found_resp = await async_client.get("/api/v1/nonexistent-endpoint")
    assert not_found_resp.status_code == 404
    req_id = not_found_resp.headers.get("X-Request-ID")
    assert req_id is not None
    uuid.UUID(req_id)
    nf_body = not_found_resp.json()
    assert nf_body["code"] == "HTTP_404"
    assert nf_body["request_id"] == req_id
    assert "detail" not in nf_body

    class ProbePayload(BaseModel):
        count: int

    @app.post("/api/v1/_test-validation")
    async def _probe_validation(payload: ProbePayload) -> dict[str, int]:
        return {"count": payload.count}

    @app.get("/api/v1/_test-domain-error")
    async def _probe_domain_error() -> None:
        raise NotFoundError(message="Order not found", code="ORDER_NOT_FOUND")

    validation_resp = await async_client.post(
        "/api/v1/_test-validation",
        json={"count": "not-an-int"},
    )
    assert validation_resp.status_code == 422
    val_req_id = validation_resp.headers.get("X-Request-ID")
    assert val_req_id is not None
    val_body = validation_resp.json()
    assert val_body["code"] == "VALIDATION_ERROR"
    assert val_body["request_id"] == val_req_id
    assert isinstance(val_body["fields"], list)
    assert len(val_body["fields"]) >= 1

    domain_resp = await async_client.get("/api/v1/_test-domain-error")
    assert domain_resp.status_code == 404
    dom_req_id = domain_resp.headers.get("X-Request-ID")
    assert dom_req_id is not None
    dom_body = domain_resp.json()
    assert dom_body["code"] == "ORDER_NOT_FOUND"
    assert dom_body["message"] == "Order not found"
    assert dom_body["request_id"] == dom_req_id


def test_production_config_rejects_default_or_short_secrets() -> None:
    with pytest.raises(ValueError):
        Settings(ENVIRONMENT="production")

    with pytest.raises(ValueError):
        Settings(
            ENVIRONMENT="production",
            JWT_SECRET="short-secret",
            QR_HMAC_SECRET="b" * 32,
            RAZORPAY_WEBHOOK_SECRET="c" * 32,
        )

    valid = Settings(
        ENVIRONMENT="production",
        JWT_SECRET="a" * 32,
        QR_HMAC_SECRET="b" * 32,
        RAZORPAY_WEBHOOK_SECRET="c" * 32,
    )
    assert valid.ENVIRONMENT == "production"
