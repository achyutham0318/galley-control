import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_secret
from app.models.audit import AuditLog
from app.models.base import AuditEvent, AuditTier, OrderStatus, UserRole
from app.models.menu import MenuItem
from app.models.restaurant import Restaurant
from app.models.user import User
from scripts.seed import seed_database


async def _setup_two_restaurants(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> dict:
    r1 = Restaurant(
        id=uuid.uuid4(),
        name=f"Rest A {uuid.uuid4().hex[:6]}",
        slug=f"rest-a-{uuid.uuid4().hex[:6]}",
        description="Restaurant A",
        location_tag="Block A",
        is_active=True,
    )
    r2 = Restaurant(
        id=uuid.uuid4(),
        name=f"Rest B {uuid.uuid4().hex[:6]}",
        slug=f"rest-b-{uuid.uuid4().hex[:6]}",
        description="Restaurant B",
        location_tag="Block B",
        is_active=True,
    )
    db_session.add_all([r1, r2])
    await db_session.flush()

    mgr_a_id = f"MGRA-{uuid.uuid4().hex[:6]}"
    cnt_a_id = f"CNTA-{uuid.uuid4().hex[:6]}"
    mgr_b_id = f"MGRB-{uuid.uuid4().hex[:6]}"
    cnt_b_id = f"CNTB-{uuid.uuid4().hex[:6]}"
    pin_h = await hash_secret("112233")

    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                staff_id=mgr_a_id,
                name="Mgr A",
                role=UserRole.MANAGER,
                restaurant_id=r1.id,
                pin_hash=pin_h,
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=cnt_a_id,
                name="Cnt A",
                role=UserRole.STAFF,
                restaurant_id=r1.id,
                pin_hash=pin_h,
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=mgr_b_id,
                name="Mgr B",
                role=UserRole.MANAGER,
                restaurant_id=r2.id,
                pin_hash=pin_h,
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=cnt_b_id,
                name="Cnt B",
                role=UserRole.STAFF,
                restaurant_id=r2.id,
                pin_hash=pin_h,
                is_active=True,
            ),
        ]
    )

    item_a = MenuItem(
        id=uuid.uuid4(),
        restaurant_id=r1.id,
        name=f"Item A {uuid.uuid4().hex[:5]}",
        description="Dish A",
        category="Meals",
        price_paise=10000,
        stock_count=20,
        estimated_prep_minutes=5,
        is_available=True,
    )
    item_b = MenuItem(
        id=uuid.uuid4(),
        restaurant_id=r2.id,
        name=f"Item B {uuid.uuid4().hex[:5]}",
        description="Dish B",
        category="Beverages",
        price_paise=5000,
        stock_count=20,
        estimated_prep_minutes=3,
        is_available=True,
    )
    db_session.add_all([item_a, item_b])
    await db_session.commit()

    async def pin_tok(sid: str) -> str:
        r = await async_client.post(
            "/api/v1/auth/staff/pin-login",
            json={"staff_id": sid, "pin": "112233"},
        )
        assert r.status_code == 200
        return r.json()["access_token"]

    email = f"mstu-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Multi Student", "password": "password123"},
    )
    assert reg.status_code == 201

    return {
        "r1": r1,
        "r2": r2,
        "item_a": item_a,
        "item_b": item_b,
        "mgr_a": await pin_tok(mgr_a_id),
        "cnt_a": await pin_tok(cnt_a_id),
        "mgr_b": await pin_tok(mgr_b_id),
        "cnt_b": await pin_tok(cnt_b_id),
        "stu": reg.json()["access_token"],
    }


async def test_seeded_restaurants_directory(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_database(db_session)
    resp = await async_client.get("/api/v1/restaurants")
    assert resp.status_code == 200
    data = resp.json()
    slugs = {r["slug"] for r in data}
    assert {"main-canteen", "south-express", "fresh-bites"}.issubset(slugs)
    for r in data:
        assert r["active_items_count"] >= 4


async def test_mixed_restaurant_cart_rejected(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    ctx = await _setup_two_restaurants(async_client, db_session)
    res = await async_client.post(
        "/api/v1/orders",
        json={
            "items": [
                {"menu_item_id": str(ctx["item_a"].id), "quantity": 1},
                {"menu_item_id": str(ctx["item_b"].id), "quantity": 1},
            ]
        },
        headers={"Authorization": f"Bearer {ctx['stu']}"},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "MIXED_RESTAURANT_CART"


async def test_cross_restaurant_qr_scan_rejected_without_consuming_nonce(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    ctx = await _setup_two_restaurants(async_client, db_session)
    order = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(ctx["item_a"].id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {ctx['stu']}"},
        )
    ).json()

    wrong_scan = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {ctx['cnt_b']}"},
    )
    assert wrong_scan.status_code == 403
    assert wrong_scan.json()["flash"] == "RED"
    assert wrong_scan.json()["reason_code"] == "WRONG_RESTAURANT"

    audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.reason_code == "WRONG_RESTAURANT")
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.tier == AuditTier.TIER_1
    assert audit.decision == "RED"

    right_scan = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {ctx['cnt_a']}"},
    )
    assert right_scan.status_code == 200
    assert right_scan.json()["flash"] == "GREEN"


async def test_cross_restaurant_unified_student_wallet(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    ctx = await _setup_two_restaurants(async_client, db_session)
    o_a = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(ctx["item_a"].id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {ctx['stu']}"},
        )
    ).json()
    v_a = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": o_a["qr_token"]},
        headers={"Authorization": f"Bearer {ctx['cnt_a']}"},
    )
    assert v_a.status_code == 200
    assert v_a.json()["points_earned"] == 10

    conv = await async_client.post(
        "/api/v1/wallet/convert-points",
        json={"coins_to_mint": 1},
        headers={"Authorization": f"Bearer {ctx['stu']}"},
    )
    assert conv.status_code == 200
    assert conv.json()["gold_coins"] == 1

    o_b = await async_client.post(
        "/api/v1/orders",
        json={
            "items": [{"menu_item_id": str(ctx["item_b"].id), "quantity": 1}],
            "coins_to_use": 1,
        },
        headers={"Authorization": f"Bearer {ctx['stu']}"},
    )
    assert o_b.status_code == 201
    assert o_b.json()["coins_redeemed"] == 1
    assert o_b.json()["cash_payable_paise"] == 4900
    assert o_b.json()["restaurant_id"] == str(ctx["r2"].id)


async def test_manager_menu_crud_archiving_and_isolation(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    ctx = await _setup_two_restaurants(async_client, db_session)

    created = await async_client.post(
        "/api/v1/manager/menu",
        json={
            "name": "Special Thali",
            "description": "Full meal",
            "category": "Meals",
            "price_paise": 15000,
            "stock_count": 25,
            "estimated_prep_minutes": 8,
            "is_available": True,
        },
        headers={"Authorization": f"Bearer {ctx['mgr_a']}"},
    )
    assert created.status_code == 201
    new_id = created.json()["id"]

    order_before_archive = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": new_id, "quantity": 1}]},
        headers={"Authorization": f"Bearer {ctx['stu']}"},
    )
    assert order_before_archive.status_code == 201

    patched = await async_client.patch(
        f"/api/v1/manager/menu/{new_id}",
        json={"price_paise": 16000, "stock_count": 10, "is_available": False},
        headers={"Authorization": f"Bearer {ctx['mgr_a']}"},
    )
    assert patched.status_code == 200
    assert patched.json()["price_paise"] == 16000
    assert patched.json()["stock_count"] == 10
    assert patched.json()["is_available"] is False

    cross_patch = await async_client.patch(
        f"/api/v1/manager/menu/{new_id}",
        json={"price_paise": 9900},
        headers={"Authorization": f"Bearer {ctx['mgr_b']}"},
    )
    assert cross_patch.status_code == 403

    archived = await async_client.delete(
        f"/api/v1/manager/menu/{new_id}",
        headers={"Authorization": f"Bearer {ctx['mgr_a']}"},
    )
    assert archived.status_code == 200
    assert archived.json()["is_archived"] is True
    assert archived.json()["is_available"] is False

    history = await async_client.get(
        "/api/v1/orders/me",
        headers={"Authorization": f"Bearer {ctx['stu']}"},
    )
    assert history.status_code == 200
    assert any(o["id"] == order_before_archive.json()["id"] for o in history.json()["items"])
