import asyncio
from datetime import datetime, timezone
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.config import settings
from app.core.security import hash_secret
from app.models.base import OrderStatus, UserRole
from app.models.menu import MenuItem
from app.models.user import User
from app.models.wallet import StudentWallet


async def _setup_wallet_test_env(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    price_paise: int = 12000,
    stock_count: int = 50,
    initial_coins: int = 0,
    initial_points: int = 0,
) -> tuple[str, str, str, uuid.UUID, MenuItem]:
    staff_id = f"CSTAFF-{uuid.uuid4().hex[:6]}"
    mgr_id = f"CMGR-{uuid.uuid4().hex[:6]}"
    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                staff_id=staff_id,
                name="Coin Test Staff",
                role=UserRole.STAFF,
                pin_hash=await hash_secret("111111"),
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=mgr_id,
                name="Coin Test Manager",
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

    email = f"cstu-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Coin Student", "password": "password123"},
    )
    assert reg.status_code == 201
    stu_tok = reg.json()["access_token"]
    stu_id = uuid.UUID(reg.json()["user"]["id"])

    if initial_coins or initial_points:
        db_session.add(
            StudentWallet(
                user_id=stu_id,
                gold_coins=initial_coins,
                loyalty_points=initial_points,
                lifetime_coins_earned=initial_coins,
                lifetime_points_earned=initial_points,
            )
        )
        await db_session.commit()

    item = MenuItem(
        id=uuid.uuid4(),
        name=f"Coin Dish {uuid.uuid4().hex[:6]}",
        description="Regression dish",
        price_paise=price_paise,
        stock_count=stock_count,
        estimated_prep_minutes=5,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()
    return stu_tok, staff_tok, mgr_tok, stu_id, item


async def test_pickup_loyalty_accrual_cash_vs_coins(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, staff_tok, _, _, item = await _setup_wallet_test_env(
        async_client, db_session, price_paise=10000, initial_coins=40
    )

    order = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 40},
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
    ).json()
    assert order["cash_payable_paise"] == 6000

    verify = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    assert verify.status_code == 200
    assert verify.json()["points_earned"] == 6

    wallet = (
        await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
    ).json()
    assert wallet["loyalty_points"] == 6
    assert wallet["gold_coins"] == 0


async def test_points_to_coins_conversion_and_insufficient_rejection(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, _, _, _, _ = await _setup_wallet_test_env(
        async_client, db_session, initial_points=35
    )

    conv = await async_client.post(
        "/api/v1/wallet/convert-points",
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert conv.status_code == 200
    assert conv.json()["gold_coins"] == 3
    assert conv.json()["loyalty_points"] == 5

    reject = await async_client.post(
        "/api/v1/wallet/convert-points",
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert reject.status_code == 400


async def test_checkout_coin_redemption_partial_and_full(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stu_tok, _, _, _, item = await _setup_wallet_test_env(
        async_client, db_session, price_paise=8000, initial_coins=110
    )

    partial = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 30},
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert partial.status_code == 201
    assert partial.json()["coins_redeemed"] == 30
    assert partial.json()["cash_payable_paise"] == 5000

    monkeypatch.setattr(settings, "RAZORPAY_MOCK_MODE", False)
    full = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 80},
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert full.status_code == 201
    assert full.json()["coins_redeemed"] == 80
    assert full.json()["cash_payable_paise"] == 0
    assert full.json()["status"] == OrderStatus.PAID.value
    assert full.json()["qr_token"] is not None


async def test_concurrent_spend_race_protection(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, _, _, stu_id, item = await _setup_wallet_test_env(
        async_client, db_session, price_paise=5000, stock_count=30, initial_coins=100
    )

    async def place_coin_order() -> int:
        resp = await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 50},
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
        return resp.status_code

    statuses = await asyncio.gather(*(place_coin_order() for _ in range(5)))
    assert statuses.count(201) == 2
    assert statuses.count(400) == 3

    db_session.expire_all()
    wallet = (
        await db_session.execute(select(StudentWallet).where(StudentWallet.user_id == stu_id))
    ).scalar_one()
    assert wallet.gold_coins == 0


async def test_instant_coin_refund_auto_and_manager_no_duplicate_credit(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, staff_tok, mgr_tok, _, item = await _setup_wallet_test_env(
        async_client, db_session, price_paise=9000, stock_count=10, initial_coins=0
    )

    clock_module.set_clock_provider(lambda: datetime(2026, 11, 25, 4, 30, 0, tzinfo=timezone.utc))
    try:
        o1 = (
            await async_client.post(
                "/api/v1/orders",
                json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
                headers={"Authorization": f"Bearer {stu_tok}"},
            )
        ).json()
        c1 = await async_client.post(
            f"/api/v1/orders/{o1['id']}/cancel",
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
        assert c1.status_code == 200
        assert c1.json()["coins_credited"] == 90

        c1_retry = await async_client.post(
            f"/api/v1/orders/{o1['id']}/cancel",
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
        assert c1_retry.status_code == 409

        qr_after = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": o1["qr_token"]},
            headers={"Authorization": f"Bearer {staff_tok}"},
        )
        assert qr_after.status_code == 409
    finally:
        clock_module.set_clock_provider(None)

    item_id = item.id
    db_session.expire_all()
    refreshed_item = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert refreshed_item.stock_count == 10

    o2 = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
    ).json()
    await async_client.post(
        f"/api/v1/kds/orders/{o2['id']}/start-cooking",
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    await async_client.post(
        f"/api/v1/orders/{o2['id']}/cancel",
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    escs = (
        await async_client.get("/api/v1/manager/escalations", headers={"Authorization": f"Bearer {mgr_tok}"})
    ).json()
    esc = next(e for e in escs if e["order_id"] == o2["id"])
    mgr_res = await async_client.post(
        f"/api/v1/manager/escalations/{esc['refund_id']}/decision",
        json={"action": "APPROVE", "notes": "Approve Gold Coins"},
        headers={"Authorization": f"Bearer {mgr_tok}"},
    )
    assert mgr_res.status_code == 200

    wallet = (
        await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
    ).json()
    assert wallet["gold_coins"] == 180


async def test_anti_farming_guarantee_coin_loop_yields_zero_points(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, staff_tok, _, _, item = await _setup_wallet_test_env(
        async_client, db_session, price_paise=10000, initial_coins=0
    )

    clock_module.set_clock_provider(lambda: datetime(2026, 11, 25, 4, 30, 0, tzinfo=timezone.utc))
    try:
        o1 = (
            await async_client.post(
                "/api/v1/orders",
                json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
                headers={"Authorization": f"Bearer {stu_tok}"},
            )
        ).json()
        c1 = await async_client.post(
            f"/api/v1/orders/{o1['id']}/cancel",
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
        assert c1.status_code == 200
        assert c1.json()["coins_credited"] == 100
    finally:
        clock_module.set_clock_provider(None)

    o2 = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 100},
            headers={"Authorization": f"Bearer {stu_tok}"},
        )
    ).json()
    assert o2["cash_payable_paise"] == 0

    pickup = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": o2["qr_token"]},
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    assert pickup.status_code == 200
    assert pickup.json()["points_earned"] == 0

    wallet = (
        await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
    ).json()
    assert wallet["loyalty_points"] == 0
    assert wallet["gold_coins"] == 0
