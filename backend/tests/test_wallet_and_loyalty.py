from datetime import datetime, timezone
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.config import settings
from app.core.security import hash_secret
from app.models.base import OrderStatus, UserRole, WalletTransactionType
from app.models.menu import MenuItem
from app.models.user import User
from app.models.wallet import StudentWallet


async def _setup_actors(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    price_paise: int = 12000,
    initial_coins: int = 0,
    initial_points: int = 0,
) -> tuple[str, str, str, uuid.UUID, MenuItem]:
    staff_id = f"WSTF-{uuid.uuid4().hex[:6]}"
    mgr_id = f"WMGR-{uuid.uuid4().hex[:6]}"
    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                staff_id=staff_id,
                name="Wallet Staff",
                role=UserRole.STAFF,
                pin_hash=await hash_secret("111111"),
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=mgr_id,
                name="Wallet Manager",
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

    email = f"wstu-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Wallet Student", "password": "password123"},
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
        name=f"Wallet Dish {uuid.uuid4().hex[:6]}",
        description="Test dish",
        price_paise=price_paise,
        stock_count=20,
        estimated_prep_minutes=5,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()
    return stu_tok, staff_tok, mgr_tok, stu_id, item


async def test_pickup_mints_loyalty_points_on_cash_payable_only(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, staff_tok, _, _, item = await _setup_actors(
        async_client, db_session, price_paise=12500, initial_coins=25
    )

    order_resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 25},
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert order_resp.status_code == 201
    order = order_resp.json()
    assert order["total_paise"] == 12500
    assert order["coins_redeemed"] == 25
    assert order["coin_discount_paise"] == 2500
    assert order["cash_payable_paise"] == 10000

    wallet_before = (
        await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
    ).json()
    assert wallet_before["gold_coins"] == 0
    assert wallet_before["loyalty_points"] == 0

    verify_resp = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_tok}"},
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["flash"] == "GREEN"
    assert verify_resp.json()["points_earned"] == 10

    wallet_after = (
        await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
    ).json()
    assert wallet_after["loyalty_points"] == 10
    assert wallet_after["convertible_coins_available"] == 1
    assert wallet_after["lifetime_points_earned"] == 10
    assert any(
        tx["tx_type"] == WalletTransactionType.POINTS_EARNED_PICKUP.value
        for tx in wallet_after["transactions"]
    )


async def test_convert_loyalty_points_and_full_coin_checkout_without_gateway(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stu_tok, _, _, _, item = await _setup_actors(
        async_client, db_session, price_paise=4000, initial_points=450
    )

    conv_resp = await async_client.post(
        "/api/v1/wallet/convert-points",
        json={"coins_to_mint": 40},
        headers={"Authorization": f"Bearer {stu_tok}", "Idempotency-Key": "conv-idem-1"},
    )
    assert conv_resp.status_code == 200
    assert conv_resp.json()["gold_coins"] == 40
    assert conv_resp.json()["loyalty_points"] == 50

    conv_replay = await async_client.post(
        "/api/v1/wallet/convert-points",
        json={"coins_to_mint": 40},
        headers={"Authorization": f"Bearer {stu_tok}", "Idempotency-Key": "conv-idem-1"},
    )
    assert conv_replay.status_code == 200
    assert conv_replay.json()["gold_coins"] == 40

    conv_all = await async_client.post(
        "/api/v1/wallet/convert-points",
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert conv_all.status_code == 200
    assert conv_all.json()["gold_coins"] == 45
    assert conv_all.json()["loyalty_points"] == 0

    monkeypatch.setattr(settings, "RAZORPAY_MOCK_MODE", False)
    order_resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 100},
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert order_resp.status_code == 201
    order = order_resp.json()
    assert order["coins_redeemed"] == 40
    assert order["cash_payable_paise"] == 0
    assert order["status"] == OrderStatus.PAID.value
    assert order["qr_token"] is not None


async def test_auto_approve_and_manager_approve_credit_instant_gold_coins(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, staff_tok, mgr_tok, _, item = await _setup_actors(
        async_client, db_session, price_paise=8000, initial_coins=0
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
        assert c1.json()["decision"] == "AUTO_APPROVE"
        assert c1.json()["status"] == "COMPLETED"
        assert c1.json()["coins_credited"] == 80
    finally:
        clock_module.set_clock_provider(None)

    w1 = (
        await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
    ).json()
    assert w1["gold_coins"] == 80
    assert w1["loyalty_points"] == 0

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
    c2 = await async_client.post(
        f"/api/v1/orders/{o2['id']}/cancel",
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert c2.status_code == 200
    assert c2.json()["decision"] == "ESCALATE"

    escs = (
        await async_client.get("/api/v1/manager/escalations", headers={"Authorization": f"Bearer {mgr_tok}"})
    ).json()
    target_esc = next(e for e in escs if e["order_id"] == o2["id"])
    mgr_dec = await async_client.post(
        f"/api/v1/manager/escalations/{target_esc['refund_id']}/decision",
        json={"action": "APPROVE", "notes": "Approved for Gold Coins"},
        headers={"Authorization": f"Bearer {mgr_tok}"},
    )
    assert mgr_dec.status_code == 200
    assert mgr_dec.json()["refund_status"] == "COMPLETED"

    w2 = (
        await async_client.get("/api/v1/wallet/me", headers={"Authorization": f"Bearer {stu_tok}"})
    ).json()
    assert w2["gold_coins"] == 160
    assert w2["loyalty_points"] == 0


async def test_wallet_never_allows_negative_balances(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    stu_tok, _, _, stu_id, item = await _setup_actors(
        async_client, db_session, price_paise=5000, initial_coins=10, initial_points=5
    )

    bad_spend = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}], "coins_to_use": 20},
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert bad_spend.status_code == 400

    bad_conv = await async_client.post(
        "/api/v1/wallet/convert-points",
        json={"coins_to_mint": 1},
        headers={"Authorization": f"Bearer {stu_tok}"},
    )
    assert bad_conv.status_code == 400

    wallet_row = (
        await db_session.execute(select(StudentWallet).where(StudentWallet.user_id == stu_id))
    ).scalar_one()
    wallet_row.gold_coins = -1
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
