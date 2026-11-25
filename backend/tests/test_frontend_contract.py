import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_secret
from app.main import app
from app.models.base import UserRole
from app.models.menu import MenuItem
from app.models.user import User


async def _create_manager_and_staff(
    async_client: httpx.AsyncClient, db_session: AsyncSession
) -> tuple[str, str]:
    mgr_id = f"MGR7-{uuid.uuid4().hex[:6]}"
    stf_id = f"STF7-{uuid.uuid4().hex[:6]}"
    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                staff_id=mgr_id,
                name="Phase 7 Manager",
                role=UserRole.MANAGER,
                pin_hash=await hash_secret("112233"),
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=stf_id,
                name="Phase 7 Staff",
                role=UserRole.STAFF,
                pin_hash=await hash_secret("445566"),
                is_active=True,
            ),
        ]
    )
    await db_session.commit()

    mgr_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": mgr_id, "pin": "112233"},
    )
    stf_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": stf_id, "pin": "445566"},
    )
    return mgr_resp.json()["access_token"], stf_resp.json()["access_token"]


async def test_openapi_schema_valid_and_contains_all_routes(
    async_client: httpx.AsyncClient,
) -> None:
    resp = await async_client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = schema["paths"]

    expected_paths = [
        "/api/v1/health",
        "/api/v1/auth/student/register",
        "/api/v1/auth/student/login",
        "/api/v1/auth/staff/pin-login",
        "/api/v1/auth/me",
        "/api/v1/auth/logout",
        "/api/v1/menu",
        "/api/v1/orders",
        "/api/v1/orders/me",
        "/api/v1/orders/{order_id}",
        "/api/v1/orders/{order_id}/refresh-qr",
        "/api/v1/orders/{order_id}/confirm-payment",
        "/api/v1/orders/{order_id}/cancel",
        "/api/v1/verify-qr",
        "/api/v1/verify-qr/manual-collect",
        "/api/v1/verify-qr/nonce-snapshot",
        "/api/v1/verify-qr/offline-sync",
        "/api/v1/kds/orders",
        "/api/v1/kds/orders/{order_id}/start-cooking",
        "/api/v1/kds/orders/{order_id}/mark-ready",
        "/api/v1/manager/escalations",
        "/api/v1/manager/escalations/{refund_id}/decision",
        "/api/v1/manager/audit-logs",
        "/api/v1/manager/metrics",
        "/api/v1/webhooks/razorpay",
        "/api/v1/demo/simulate/{scenario}",
    ]
    for path in expected_paths:
        assert path in paths


async def test_phase7_additive_fields_and_audit_filters(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    mgr_token, staff_token = await _create_manager_and_staff(
        async_client, db_session
    )
    email = f"p7stu-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Dev K.", "password": "password123"},
    )
    student_token = reg.json()["access_token"]

    item = MenuItem(
        id=uuid.uuid4(),
        name=f"P7 Paneer Thali {uuid.uuid4().hex[:6]}",
        description="Deluxe thali",
        price_paise=32000,
        stock_count=10,
        estimated_prep_minutes=6,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()

    order = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {student_token}"},
        )
    ).json()

    await async_client.post(
        f"/api/v1/kds/orders/{order['id']}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    await async_client.post(
        f"/api/v1/orders/{order['id']}/cancel",
        headers={"Authorization": f"Bearer {student_token}"},
    )

    esc_resp = await async_client.get(
        "/api/v1/manager/escalations",
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert esc_resp.status_code == 200
    matched = [e for e in esc_resp.json() if e["order_id"] == order["id"]]
    assert len(matched) == 1
    assert matched[0]["total_paise"] == 32000
    assert matched[0]["student_email"] == email
    assert matched[0]["reason_code"] == "STAGE_GUARD_ESCALATION"
    assert matched[0]["human_message"] is not None

    filtered_audits = await async_client.get(
        "/api/v1/manager/audit-logs?event_type=REFUND_EVAL&decision=ESCALATE",
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert filtered_audits.status_code == 200
    audit_items = filtered_audits.json()["items"]
    assert len(audit_items) >= 1
    assert all(a["decision"] == "ESCALATE" for a in audit_items)
    assert any(a["order_id"] == order["id"] and a["total_paise"] == 32000 for a in audit_items)

    metrics_resp = await async_client.get(
        "/api/v1/manager/metrics",
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert metrics_resp.status_code == 200
    m = metrics_resp.json()
    assert "verified_scans_count" in m
    assert "avg_refund_eval_latency_ms" in m
    assert "last_blocked_order_number" in m
    assert "last_blocked_at" in m
    assert "last_blocked_at_ist" in m


async def test_cors_preflight_allowed_and_rejected_origins(
    async_client: httpx.AsyncClient,
) -> None:
    for origin in settings.CORS_ORIGINS:
        for method in ("GET", "POST"):
            resp = await async_client.options(
                "/api/v1/orders",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "Authorization,Content-Type,Idempotency-Key",
                },
            )
            assert resp.status_code == 200
            assert resp.headers.get("access-control-allow-origin") == origin
            assert "access-control-allow-credentials" not in resp.headers

    blocked_resp = await async_client.options(
        "/api/v1/orders",
        headers={
            "Origin": "https://untrusted-origin.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization,Content-Type",
        },
    )
    assert "access-control-allow-origin" not in blocked_resp.headers
