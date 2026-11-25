from datetime import datetime, timedelta, timezone
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.security import hash_secret
from app.models.audit import AuditLog
from app.models.base import AuditEvent, AuditTier, UserRole
from app.models.menu import MenuItem
from app.models.user import User


async def _setup_surplus_env(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    price_paise: int = 10000,
    stock_count: int = 25,
) -> tuple[str, str, str, MenuItem]:
    staff_id = f"SSTF-{uuid.uuid4().hex[:6]}"
    mgr_id = f"SMGR-{uuid.uuid4().hex[:6]}"
    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                staff_id=staff_id,
                name="Surplus Staff",
                role=UserRole.STAFF,
                pin_hash=await hash_secret("111111"),
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=mgr_id,
                name="Surplus Manager",
                role=UserRole.MANAGER,
                pin_hash=await hash_secret("222222"),
                is_active=True,
            ),
        ]
    )
    await db_session.commit()

    staff_tok = (
        await async_client.post(
            "/api/v1/auth/staff/pin-login",
            json={"staff_id": staff_id, "pin": "111111"},
        )
    ).json()["access_token"]
    mgr_tok = (
        await async_client.post(
            "/api/v1/auth/staff/pin-login",
            json={"staff_id": mgr_id, "pin": "222222"},
        )
    ).json()["access_token"]

    email = f"sstu-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Surplus Student", "password": "password123"},
    )
    assert reg.status_code == 201
    stu_tok = reg.json()["access_token"]

    item = MenuItem(
        id=uuid.uuid4(),
        name=f"Surplus Dish {uuid.uuid4().hex[:6]}",
        description="Bulk excess item",
        price_paise=price_paise,
        stock_count=stock_count,
        estimated_prep_minutes=5,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()
    return stu_tok, staff_tok, mgr_tok, item


async def test_surplus_deal_rbac_and_audit_log(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, staff_tok, mgr_tok, item = await _setup_surplus_env(async_client, db_session)
    payload = {
        "is_surplus_deal": True,
        "discount_percent": 25,
        "surplus_note": "Fresh batch excess",
        "expires_in_minutes": 60,
    }

    stu_res = await async_client.post(
        f"/api/v1/manager/menu/{item.id}/surplus-deal",
        json=payload,
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert stu_res.status_code == 403

    staff_res = await async_client.post(
        f"/api/v1/manager/menu/{item.id}/surplus-deal",
        json=payload,
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    assert staff_res.status_code == 403

    mgr_res = await async_client.post(
        f"/api/v1/manager/menu/{item.id}/surplus-deal",
        json=payload,
        headers={"Authorization": f"Bearer {mgr_tok}"},
    )
    assert mgr_res.status_code == 200
    body = mgr_res.json()
    assert body["is_surplus_active"] is True
    assert body["active_discount_percent"] == 25
    assert body["effective_price_paise"] == 7500
    assert body["is_bulk_excess"] is True

    audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.event_type == AuditEvent.SURPLUS_DISCOUNT_UPDATE)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.tier == AuditTier.HUMAN_OVERRIDE
    assert audit.input_state["new_discount_percent"] == 25


async def test_server_side_surplus_pricing_and_expiry_fallback(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, _, mgr_tok, item = await _setup_surplus_env(
        async_client, db_session, price_paise=10000, stock_count=20
    )

    t0 = datetime(2026, 11, 25, 4, 0, 0, tzinfo=timezone.utc)
    clock_module.set_clock_provider(lambda: t0)
    try:
        res = await async_client.post(
            f"/api/v1/manager/menu/{item.id}/surplus-deal",
            json={"is_surplus_deal": True, "discount_percent": 30, "expires_in_minutes": 30},
            headers={"Authorization": f"Bearer {mgr_tok}"},
        )
        assert res.status_code == 200

        order_disc = await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 2}]},
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
        assert order_disc.status_code == 201
        d_body = order_disc.json()
        assert d_body["total_paise"] == 14000
        assert d_body["items"][0]["unit_price_paise"] == 7000
        assert d_body["items"][0]["original_unit_price_paise"] == 10000
        assert d_body["items"][0]["discount_percent_applied"] == 30

        clock_module.set_clock_provider(lambda: t0 + timedelta(minutes=31))
        order_full = await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
        assert order_full.status_code == 201
        f_body = order_full.json()
        assert f_body["total_paise"] == 10000
        assert f_body["items"][0]["unit_price_paise"] == 10000
        assert f_body["items"][0]["discount_percent_applied"] == 0
    finally:
        clock_module.set_clock_provider(None)


async def test_anti_arbitrage_refund_and_waste_metrics(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, staff_tok, mgr_tok, item = await _setup_surplus_env(
        async_client, db_session, price_paise=10000, stock_count=20
    )

    clock_module.set_clock_provider(lambda: datetime(2026, 11, 25, 4, 30, 0, tzinfo=timezone.utc))
    try:
        await async_client.post(
            f"/api/v1/manager/menu/{item.id}/surplus-deal",
            json={"is_surplus_deal": True, "discount_percent": 30},
            headers={"Authorization": f"Bearer {mgr_tok}"},
        )

        o_cancel = (
            await async_client.post(
                "/api/v1/orders",
                json={"items": [{"menu_item_id": str(item.id), "quantity": 2}]},
                headers={"Authorization": f"Bearer {stu_tok}"},
            )
        ).json()
        assert o_cancel["total_paise"] == 14000

        cancel_res = await async_client.post(
            f"/api/v1/orders/{o_cancel['id']}/cancel",
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
        assert cancel_res.status_code == 200
        assert cancel_res.json()["coins_credited"] == 140

        wallet = (
            await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
        ).json()
        assert wallet["gold_coins"] == 140

        item_id = item.id
        db_session.expire_all()
        db_item = (await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))).scalar_one()
        assert db_item.stock_count == 20

        o_kept = (
            await async_client.post(
                "/api/v1/orders",
                json={"items": [{"menu_item_id": str(item.id), "quantity": 2}]},
                headers={"Authorization": f"Bearer {stu_tok}"},
            )
        ).json()
        await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": o_kept["qr_token"]},
            headers={"Authorization": f"Bearer {staff_tok}"},
        )

        metrics = (
            await async_client.get("/api/v1/manager/metrics", headers={"Authorization": f"Bearer {mgr_tok}"})
        ).json()
        assert metrics["active_surplus_deals_count"] >= 1
        assert metrics["surplus_units_rescued_count"] >= 2
        assert metrics["food_waste_prevented_paise"] >= 14000
    finally:
        clock_module.set_clock_provider(None)
