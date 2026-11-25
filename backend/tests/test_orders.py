import asyncio
from datetime import timedelta
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.config import settings
from app.core.security import hash_secret
from app.models.base import OrderStatus, PrepStage, UserRole
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.user import User
from app.services.qr_service import verify_qr_token_signature
from app.services.stock_service import restore_stock


async def _register_student(async_client: httpx.AsyncClient, prefix: str) -> tuple[str, uuid.UUID]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@campus.edu"
    resp = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": f"Student {prefix}", "password": "password123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["access_token"], uuid.UUID(data["user"]["id"])


async def _create_menu_item(
    db_session: AsyncSession,
    name: str,
    price_paise: int,
    stock_count: int,
    prep_minutes: int = 5,
) -> MenuItem:
    item = MenuItem(
        id=uuid.uuid4(),
        name=name,
        description=f"Desc {name}",
        price_paise=price_paise,
        stock_count=stock_count,
        estimated_prep_minutes=prep_minutes,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()
    return item


async def test_concurrent_last_stock_reservation(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    item = await _create_menu_item(db_session, "Last Thali", 12000, stock_count=1)
    tokens = []
    for idx in range(5):
        token, _ = await _register_student(async_client, f"race{idx}")
        tokens.append(token)

    async def place_order(token: str) -> httpx.Response:
        return await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {token}"},
        )

    responses = await asyncio.gather(*(place_order(t) for t in tokens))
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4

    item_id = item.id
    db_session.expire_all()
    refreshed = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert refreshed.stock_count == 0


async def test_concurrent_opposite_order_no_deadlock(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    item_a = await _create_menu_item(db_session, "Item A", 5000, stock_count=10)
    item_b = await _create_menu_item(db_session, "Item B", 6000, stock_count=10)
    token_1, _ = await _register_student(async_client, "dl1")
    token_2, _ = await _register_student(async_client, "dl2")

    req_1 = async_client.post(
        "/api/v1/orders",
        json={
            "items": [
                {"menu_item_id": str(item_a.id), "quantity": 1},
                {"menu_item_id": str(item_b.id), "quantity": 1},
            ]
        },
        headers={"Authorization": f"Bearer {token_1}"},
    )
    req_2 = async_client.post(
        "/api/v1/orders",
        json={
            "items": [
                {"menu_item_id": str(item_b.id), "quantity": 1},
                {"menu_item_id": str(item_a.id), "quantity": 1},
            ]
        },
        headers={"Authorization": f"Bearer {token_2}"},
    )

    resp_1, resp_2 = await asyncio.gather(req_1, req_2)
    assert resp_1.status_code == 201
    assert resp_2.status_code == 201


async def test_idempotency_key_returns_existing_order_and_single_decrement(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    item = await _create_menu_item(db_session, "Idempotent Dosa", 8000, stock_count=5)
    token, _ = await _register_student(async_client, "idem")
    idem_key = f"idem-{uuid.uuid4()}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": idem_key,
    }

    first = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 2}]},
        headers=headers,
    )
    second = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 2}]},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    item_id = item.id
    db_session.expire_all()
    refreshed = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert refreshed.stock_count == 3


async def test_order_price_ignored_and_validation_rejects_bad_inputs(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    item = await _create_menu_item(db_session, "Priced Coffee", 4500, stock_count=10)
    token, _ = await _register_student(async_client, "pricecheck")
    headers = {"Authorization": f"Bearer {token}"}

    order_resp = await async_client.post(
        "/api/v1/orders",
        json={
            "items": [
                {
                    "menu_item_id": str(item.id),
                    "quantity": 2,
                    "unit_price_paise": 1,
                }
            ]
        },
        headers=headers,
    )
    assert order_resp.status_code == 201
    assert order_resp.json()["total_paise"] == 9000

    dup_resp = await async_client.post(
        "/api/v1/orders",
        json={
            "items": [
                {"menu_item_id": str(item.id), "quantity": 1},
                {"menu_item_id": str(item.id), "quantity": 1},
            ]
        },
        headers=headers,
    )
    assert dup_resp.status_code == 422

    zero_resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 0}]},
        headers=headers,
    )
    assert zero_resp.status_code == 422

    eleven_resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 11}]},
        headers=headers,
    )
    assert eleven_resp.status_code == 422


async def test_mock_order_paid_with_verifiable_qr_and_no_secrets(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    item = await _create_menu_item(db_session, "QR Meal", 15000, stock_count=5)
    token, _ = await _register_student(async_client, "qrcheck")

    resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "PAID"
    assert body["order_number"].startswith("GC-")
    assert body["qr_token"] is not None
    assert body["qr_seconds_remaining"] > 0

    raw_text = resp.text
    assert settings.QR_HMAC_SECRET not in raw_text
    assert "pin_hash" not in raw_text
    assert "password_hash" not in raw_text

    parsed_order_id, _, _ = verify_qr_token_signature(body["qr_token"])
    assert parsed_order_id == body["id"]


async def test_menu_current_wait_minutes_formula(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    fast_item = await _create_menu_item(db_session, "Fast Tea", 2000, stock_count=20, prep_minutes=3)
    slow_item = await _create_menu_item(db_session, "Slow Biryani", 18000, stock_count=20, prep_minutes=12)
    _, student_id = await _register_student(async_client, "waitcalc")

    current_time = now()
    order_1 = Order(
        id=uuid.uuid4(),
        order_number=f"GC-WAIT1-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=20000,
        status=OrderStatus.PAID,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=current_time + timedelta(minutes=5),
    )
    order_2 = Order(
        id=uuid.uuid4(),
        order_number=f"GC-WAIT2-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=2000,
        status=OrderStatus.PAID,
        prep_stage=PrepStage.IN_PREP,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=current_time + timedelta(minutes=5),
    )
    db_session.add_all([order_1, order_2])
    db_session.add_all(
        [
            OrderItem(
                id=uuid.uuid4(),
                order_id=order_1.id,
                menu_item_id=fast_item.id,
                item_name_snapshot=fast_item.name,
                quantity=1,
                unit_price_paise=fast_item.price_paise,
            ),
            OrderItem(
                id=uuid.uuid4(),
                order_id=order_1.id,
                menu_item_id=slow_item.id,
                item_name_snapshot=slow_item.name,
                quantity=1,
                unit_price_paise=slow_item.price_paise,
            ),
            OrderItem(
                id=uuid.uuid4(),
                order_id=order_2.id,
                menu_item_id=fast_item.id,
                item_name_snapshot=fast_item.name,
                quantity=1,
                unit_price_paise=fast_item.price_paise,
            ),
        ]
    )
    await db_session.commit()

    menu_resp = await async_client.get("/api/v1/menu")
    assert menu_resp.status_code == 200
    items = menu_resp.json()
    assert len(items) >= 2
    assert items[0]["current_wait_minutes"] >= 8


async def test_order_detail_rbac_and_restore_stock(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    item = await _create_menu_item(db_session, "RBAC Bowl", 11000, stock_count=5)
    owner_token, _ = await _register_student(async_client, "owner")
    other_token, _ = await _register_student(async_client, "other")

    staff_id = f"ORDSTF-{uuid.uuid4().hex[:8]}"
    staff_user = User(
        id=uuid.uuid4(),
        staff_id=staff_id,
        name="Order Staff",
        role=UserRole.STAFF,
        pin_hash=await hash_secret("246810"),
        is_active=True,
    )
    db_session.add(staff_user)
    await db_session.commit()

    staff_login = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "246810"},
    )
    assert staff_login.status_code == 200
    staff_token = staff_login.json()["access_token"]

    create_resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 2}]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert create_resp.status_code == 201
    order_id = create_resp.json()["id"]

    other_resp = await async_client.get(
        f"/api/v1/orders/{order_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert other_resp.status_code == 403

    owner_resp = await async_client.get(
        f"/api/v1/orders/{order_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert owner_resp.status_code == 200

    staff_resp = await async_client.get(
        f"/api/v1/orders/{order_id}",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert staff_resp.status_code == 200

    await restore_stock(db_session, [(item.id, 2)])
    await db_session.commit()
    item_id = item.id
    db_session.expire_all()
    restored_item = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert restored_item.stock_count == 5
