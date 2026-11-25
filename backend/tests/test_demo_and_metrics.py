from datetime import datetime, timedelta, timezone
import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.config import settings
from app.core.security import hash_secret
from app.models.audit import AuditLog
from app.models.base import OrderStatus, PrepStage, UserRole
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.refund import Refund
from app.models.user import User
from scripts.seed import seed_database


async def _create_manager_and_staff_tokens(
    async_client: httpx.AsyncClient, db_session: AsyncSession
) -> tuple[str, str]:
    mgr_id = f"MGR6-{uuid.uuid4().hex[:6]}"
    stf_id = f"STF6-{uuid.uuid4().hex[:6]}"
    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                staff_id=mgr_id,
                name="Phase 6 Manager",
                role=UserRole.MANAGER,
                pin_hash=await hash_secret("112233"),
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=stf_id,
                name="Phase 6 Staff",
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


async def test_seeder_idempotent_and_blocked_in_production(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_database(db_session)

    users_count_1 = int((await db_session.execute(select(func.count(User.id)))).scalar_one())
    menu_count_1 = int((await db_session.execute(select(func.count(MenuItem.id)))).scalar_one())
    orders_count_1 = int((await db_session.execute(select(func.count(Order.id)))).scalar_one())
    refunds_count_1 = int((await db_session.execute(select(func.count(Refund.id)))).scalar_one())

    await seed_database(db_session)

    users_count_2 = int((await db_session.execute(select(func.count(User.id)))).scalar_one())
    menu_count_2 = int((await db_session.execute(select(func.count(MenuItem.id)))).scalar_one())
    orders_count_2 = int((await db_session.execute(select(func.count(Order.id)))).scalar_one())
    refunds_count_2 = int((await db_session.execute(select(func.count(Refund.id)))).scalar_one())

    assert users_count_1 == users_count_2
    assert menu_count_1 == menu_count_2
    assert orders_count_1 == orders_count_2
    assert refunds_count_1 == refunds_count_2

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        await seed_database(db_session)


async def test_demo_simulator_all_four_scenarios_and_guards(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr_token, staff_token = await _create_manager_and_staff_tokens(
        async_client, db_session
    )

    monkeypatch.setattr(settings, "DEMO_MODE_ENABLED", False)
    disabled_resp = await async_client.post(
        "/api/v1/demo/simulate/normal_pickup",
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert disabled_resp.status_code == 404

    monkeypatch.setattr(settings, "DEMO_MODE_ENABLED", True)

    staff_forbidden = await async_client.post(
        "/api/v1/demo/simulate/normal_pickup",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert staff_forbidden.status_code == 403

    r_normal = await async_client.post(
        "/api/v1/demo/simulate/normal_pickup",
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert r_normal.status_code == 200
    b_normal = r_normal.json()
    assert b_normal["total_latency_ms"] < 500.0
    assert b_normal["final_result"]["flash"] == "GREEN"
    assert len(b_normal["audit_entries"]) >= 3

    r_replay = await async_client.post(
        "/api/v1/demo/simulate/qr_replay",
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert r_replay.status_code == 200
    b_replay = r_replay.json()
    assert b_replay["total_latency_ms"] < 500.0
    assert b_replay["steps"][1]["status_code"] == 200
    assert b_replay["steps"][2]["status_code"] == 409
    assert b_replay["final_result"]["flash"] == "RED"

    r_pre_prep = await async_client.post(
        "/api/v1/demo/simulate/pre_prep_cancel",
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert r_pre_prep.status_code == 200
    b_pre = r_pre_prep.json()
    assert b_pre["total_latency_ms"] < 500.0
    assert b_pre["final_result"]["decision"] == "AUTO_APPROVE"
    assert b_pre["final_result"]["risk_score"] < 0.3

    r_abuse_surge = await async_client.post(
        "/api/v1/demo/simulate/serial_abuse",
        json={"surge_override": True},
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert r_abuse_surge.status_code == 200
    b_surge = r_abuse_surge.json()
    assert b_surge["total_latency_ms"] < 500.0
    assert b_surge["final_result"]["risk_score"] == 0.75
    assert b_surge["final_result"]["decision"] == "BLOCK"

    r_abuse_quiet = await async_client.post(
        "/api/v1/demo/simulate/serial_abuse",
        json={"surge_override": False},
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert r_abuse_quiet.status_code == 200
    b_quiet = r_abuse_quiet.json()
    assert b_quiet["total_latency_ms"] < 500.0
    assert b_quiet["final_result"]["risk_score"] == 0.55
    assert b_quiet["final_result"]["decision"] == "ESCALATE"


async def test_metrics_scan_compliance_and_no_double_count_revenue_protected(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    mgr_token, staff_token = await _create_manager_and_staff_tokens(
        async_client, db_session
    )

    isolated_day = datetime(2026, 11, 20, 6, 0, 0, tzinfo=timezone.utc)
    clock_module.set_clock_provider(lambda: isolated_day)
    try:
        empty_metrics = await async_client.get(
            "/api/v1/manager/metrics",
            headers={"Authorization": f"Bearer {mgr_token}"},
        )
        assert empty_metrics.status_code == 200
        assert empty_metrics.json()["scan_compliance_rate"] == 100.0

        email = f"metstu-{uuid.uuid4().hex[:8]}@campus.edu"
        reg = await async_client.post(
            "/api/v1/auth/student/register",
            json={"email": email, "name": "Metrics Student", "password": "password123"},
        )
        student_token = reg.json()["access_token"]
        student_id = uuid.UUID(reg.json()["user"]["id"])

        item = MenuItem(
            id=uuid.uuid4(),
            name=f"MetricsDish {uuid.uuid4().hex[:6]}",
            description="Metrics",
            price_paise=15000,
            stock_count=20,
            estimated_prep_minutes=5,
            is_available=True,
        )
        db_session.add(item)
        await db_session.commit()

        ord_scan = (
            await async_client.post(
                "/api/v1/orders",
                json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
                headers={"Authorization": f"Bearer {student_token}"},
            )
        ).json()
        await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": ord_scan["qr_token"]},
            headers={"Authorization": f"Bearer {staff_token}"},
        )

        ord_manual = (
            await async_client.post(
                "/api/v1/orders",
                json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
                headers={"Authorization": f"Bearer {student_token}"},
            )
        ).json()
        await async_client.post(
            "/api/v1/verify-qr/manual-collect",
            json={"order_number": ord_manual["order_number"], "reason": "Screen cracked"},
            headers={"Authorization": f"Bearer {staff_token}"},
        )

        for idx in range(4):
            prior_ord = Order(
                id=uuid.uuid4(),
                order_number=f"GC-MP-{uuid.uuid4().hex[:6]}-{idx}",
                user_id=student_id,
                total_paise=5000,
                status=OrderStatus.REFUNDED,
                prep_stage=PrepStage.RECEIVED,
                qr_nonce=str(uuid.uuid4()),
                qr_expires_at=isolated_day - timedelta(days=1),
                created_at=isolated_day - timedelta(days=2),
                updated_at=isolated_day - timedelta(days=2),
            )
            db_session.add(prior_ord)
            await db_session.flush()
            db_session.add(
                Refund(
                    id=uuid.uuid4(),
                    order_id=prior_ord.id,
                    user_id=student_id,
                    prep_stage_at_request=PrepStage.RECEIVED,
                    refund_count_7d=idx,
                    surge_active=False,
                    risk_score=0.0,
                    factor_breakdown={"s_prep_norm": 0.0, "v_user_norm": 0.0, "l_surge": 0.0},
                    decision="AUTO_APPROVE",
                    status="COMPLETED",
                    reason_code="RISK_LOW_PRE_PREP",
                    human_message="Prior",
                    created_at=isolated_day - timedelta(days=2),
                )
            )
        await db_session.commit()

        ord_protected = (
            await async_client.post(
                "/api/v1/orders",
                json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
                headers={"Authorization": f"Bearer {student_token}"},
            )
        ).json()
        await async_client.post(
            f"/api/v1/kds/orders/{ord_protected['id']}/start-cooking",
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        await async_client.post(
            f"/api/v1/kds/orders/{ord_protected['id']}/mark-ready",
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        cancel_blk = await async_client.post(
            f"/api/v1/orders/{ord_protected['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
        assert cancel_blk.json()["decision"] == "BLOCK"

        await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": ord_protected["qr_token"]},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": ord_protected["qr_token"]},
            headers={"Authorization": f"Bearer {staff_token}"},
        )

        metrics_resp = await async_client.get(
            "/api/v1/manager/metrics",
            headers={"Authorization": f"Bearer {mgr_token}"},
        )
        assert metrics_resp.status_code == 200
        m = metrics_resp.json()
        assert m["scan_compliance_rate"] == 66.67
        assert m["revenue_protected_paise"] == 15000
        assert m["qr_attacks_blocked_count"] >= 1
    finally:
        clock_module.set_clock_provider(None)


async def test_audit_logs_pagination_and_cors_origins(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    mgr_token, staff_token = await _create_manager_and_staff_tokens(
        async_client, db_session
    )

    audit_resp = await async_client.get(
        "/api/v1/manager/audit-logs?limit=5",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert audit_resp.status_code == 200
    body = audit_resp.json()
    assert len(body["items"]) <= 5
    if body["items"]:
        assert body["items"][0]["created_at"].endswith("Z")
        assert body["items"][0]["created_at_ist"].endswith("IST")

    allowed_origin = settings.CORS_ORIGINS[0]
    cors_ok = await async_client.options(
        "/api/v1/health",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert cors_ok.headers.get("access-control-allow-origin") == allowed_origin

    cors_bad = await async_client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in cors_bad.headers
