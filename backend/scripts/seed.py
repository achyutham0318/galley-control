import asyncio
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now, to_ist
from app.core.config import settings
from app.core.database import async_session_maker
from app.core.security import hash_secret
from app.models.audit import AuditLog
from app.models.base import (
    AuditEvent,
    AuditTier,
    OrderStatus,
    PrepStage,
    RefundDecision,
    RefundStatus,
    UserRole,
    WalletTransactionType,
)
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.refund import Refund
from app.models.restaurant import DEFAULT_RESTAURANT_ID, Restaurant
from app.models.user import User
from app.models.wallet import StudentWallet, WalletTransaction

_SOUTH_RESTAURANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FRESH_RESTAURANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")

_MENU_CATALOG = [
    ("Paneer Roll", "Grilled spiced paneer wrapped in flaky paratha", "Wraps", 11000, 45, 6),
    ("Veg Burger", "Crispy vegetable patty burger with mint mayo", "Snacks", 9000, 50, 5),
    ("Masala Dosa", "Crisp rice crepe with spiced potato filling and chutney", "South Indian", 8000, 40, 7),
    ("Chole Bhature", "Spiced chickpea curry with two fluffy bhaturas", "Meals", 12000, 35, 8),
    ("Rajma Chawal Bowl", "Home-style kidney bean curry over steamed basmati rice", "Meals", 10000, 40, 5),
    ("Cold Coffee", "Chilled blended espresso with milk and cocoa", "Beverages", 6000, 60, 3),
    ("Samosa Plate", "Two crispy potato samosas with tamarind chutney", "Snacks", 4000, 80, 2),
    ("Veg Biryani", "Fragrant saffron basmati rice with seasonal vegetables", "Meals", 14000, 30, 10),
]

_SOUTH_MENU_CATALOG = [
    ("Idli Sambar", "Steamed rice cakes served with hot lentil sambar and chutney", "South Indian", 5000, 45, 4),
    ("Medu Vada", "Crispy golden lentil doughnuts with coconut chutney", "South Indian", 6000, 35, 5),
    ("Filter Coffee", "Traditional frothy South Indian degree filter coffee", "Beverages", 3500, 60, 2),
    ("Rava Masala Dosa", "Crispy semolina crepe stuffed with spiced potato masala", "South Indian", 9500, 25, 7),
]

_FRESH_MENU_CATALOG = [
    ("Watermelon Juice", "Freshly cold-pressed chilled watermelon juice", "Beverages", 5000, 50, 2),
    ("Mango Smoothie", "Thick Alphonso mango yogurt smoothie", "Beverages", 8000, 30, 3),
    ("Grilled Veg Sandwich", "Multi-grain toasted sandwich with pesto and veggies", "Snacks", 7500, 35, 5),
    ("Seasonal Fruit Bowl", "Freshly cut seasonal fruit bowl with chaat masala", "Healthy", 6500, 28, 3),
]


async def seed_database(db: AsyncSession | None = None) -> dict[str, int]:
    if settings.ENVIRONMENT.lower() == "production":
        raise RuntimeError("Refusing to run synthetic seeder when ENVIRONMENT=production")

    owns_session = db is None
    if db is None:
        session_cm = async_session_maker()
        db = await session_cm.__aenter__()

    try:
        current_time = now()
        dialect_insert = (
            sqlite_insert
            if db.bind is not None and db.bind.dialect.name == "sqlite"
            else pg_insert
        )

        restaurant_specs = [
            (
                DEFAULT_RESTAURANT_ID,
                "Main Campus Canteen",
                "main-canteen",
                "Primary campus food court serving multi-cuisine meals and snacks",
                "Central Food Court",
            ),
            (
                _SOUTH_RESTAURANT_ID,
                "South Express Counter",
                "south-express",
                "Authentic South Indian dosas, idlis, and filter coffee",
                "Engineering Block A",
            ),
            (
                _FRESH_RESTAURANT_ID,
                "Fresh Bites & Juice Bar",
                "fresh-bites",
                "Cold-pressed juices, smoothies, sandwiches, and fruit bowls",
                "Library Plaza",
            ),
        ]
        for rid, rname, rslug, rdesc, rloc in restaurant_specs:
            await db.execute(
                dialect_insert(Restaurant)
                .values(
                    id=rid,
                    name=rname,
                    slug=rslug,
                    description=rdesc,
                    location_tag=rloc,
                    is_active=True,
                    created_at=current_time,
                )
                .on_conflict_do_nothing(index_elements=["slug"])
            )

        for name, desc, cat, price, stock, prep_min in _MENU_CATALOG:
            stmt = (
                dialect_insert(MenuItem)
                .values(
                    id=uuid.uuid4(),
                    restaurant_id=DEFAULT_RESTAURANT_ID,
                    name=name,
                    description=desc,
                    category=cat,
                    price_paise=price,
                    stock_count=stock,
                    estimated_prep_minutes=prep_min,
                    is_available=True,
                    is_archived=False,
                    updated_at=current_time,
                )
                .on_conflict_do_nothing(index_elements=["restaurant_id", "name"])
            )
            await db.execute(stmt)

        for rid, catalog in (
            (_SOUTH_RESTAURANT_ID, _SOUTH_MENU_CATALOG),
            (_FRESH_RESTAURANT_ID, _FRESH_MENU_CATALOG),
        ):
            for name, desc, cat, price, stock, prep_min in catalog:
                await db.execute(
                    dialect_insert(MenuItem)
                    .values(
                        id=uuid.uuid4(),
                        restaurant_id=rid,
                        name=name,
                        description=desc,
                        category=cat,
                        price_paise=price,
                        stock_count=stock,
                        estimated_prep_minutes=prep_min,
                        is_available=True,
                        is_archived=False,
                        updated_at=current_time,
                    )
                    .on_conflict_do_nothing(index_elements=["restaurant_id", "name"])
                )

        staff_accounts = [
            ("MGR01", "Campus Canteen Manager", UserRole.MANAGER, "112233", DEFAULT_RESTAURANT_ID),
            ("COUNTER01", "Counter Tablet Operator", UserRole.STAFF, "445566", DEFAULT_RESTAURANT_ID),
            ("KITCHEN01", "Kitchen KDS Lead", UserRole.STAFF, "778899", DEFAULT_RESTAURANT_ID),
            ("MGR02", "South Express Manager", UserRole.MANAGER, "223344", _SOUTH_RESTAURANT_ID),
            ("COUNTER02", "South Express Counter", UserRole.STAFF, "556677", _SOUTH_RESTAURANT_ID),
            ("KITCHEN02", "South Express Kitchen", UserRole.STAFF, "889900", _SOUTH_RESTAURANT_ID),
            ("MGR03", "Fresh Bites Manager", UserRole.MANAGER, "334455", _FRESH_RESTAURANT_ID),
            ("COUNTER03", "Fresh Bites Counter", UserRole.STAFF, "667788", _FRESH_RESTAURANT_ID),
            ("KITCHEN03", "Fresh Bites Kitchen", UserRole.STAFF, "990011", _FRESH_RESTAURANT_ID),
        ]
        for staff_id, name, role, pin, rid in staff_accounts:
            pin_hash = await hash_secret(pin)
            stmt = (
                dialect_insert(User)
                .values(
                    id=uuid.uuid4(),
                    staff_id=staff_id,
                    name=name,
                    role=role,
                    restaurant_id=rid,
                    pin_hash=pin_hash,
                    is_active=True,
                    created_at=current_time,
                )
                .on_conflict_do_nothing(index_elements=["staff_id"])
            )
            await db.execute(stmt)

        student_password_hash = await hash_secret("Student@123")
        clean_emails = [f"student{i:02d}@campus.edu" for i in range(1, 11)]
        abuser_emails = [f"abuser{i:02d}@campus.edu" for i in range(1, 4)]

        for idx, email in enumerate(clean_emails, start=1):
            stmt = (
                dialect_insert(User)
                .values(
                    id=uuid.uuid4(),
                    email=email,
                    name=f"Student {idx:02d}",
                    role=UserRole.STUDENT,
                    password_hash=student_password_hash,
                    is_active=True,
                    created_at=current_time,
                )
                .on_conflict_do_nothing(index_elements=["email"])
            )
            await db.execute(stmt)

        for idx, email in enumerate(abuser_emails, start=1):
            stmt = (
                dialect_insert(User)
                .values(
                    id=uuid.uuid4(),
                    email=email,
                    name=f"Serial Abuser {idx:02d}",
                    role=UserRole.STUDENT,
                    password_hash=student_password_hash,
                    is_active=True,
                    created_at=current_time,
                )
                .on_conflict_do_nothing(index_elements=["email"])
            )
            await db.execute(stmt)

        await db.flush()

        menu_items = list(
            (
                await db.execute(
                    select(MenuItem)
                    .where(MenuItem.restaurant_id == DEFAULT_RESTAURANT_ID)
                    .order_by(MenuItem.name.asc())
                )
            ).scalars().all()
        )
        clean_users = list(
            (
                await db.execute(
                    select(User).where(User.email.in_(clean_emails)).order_by(User.email.asc())
                )
            )
            .scalars()
            .all()
        )
        abuser_users = list(
            (
                await db.execute(
                    select(User).where(User.email.in_(abuser_emails)).order_by(User.email.asc())
                )
            )
            .scalars()
            .all()
        )

        ist_today = to_ist(current_time).date()
        surge_base_utc = datetime(
            ist_today.year, ist_today.month, ist_today.day, 6, 45, 0, tzinfo=timezone.utc
        )

        order_specs: list[tuple[str, User, OrderStatus, PrepStage, datetime, bool]] = []
        seq = 1

        for idx, abuser in enumerate(abuser_users):
            refund_total = 4 if idx < 2 else 3
            for r_idx in range(refund_total):
                order_num = f"GC-SEED-{1000 + seq}"
                ts = current_time - timedelta(days=(r_idx + 1), hours=idx)
                order_specs.append(
                    (order_num, abuser, OrderStatus.REFUNDED, PrepStage.RECEIVED, ts, False)
                )
                seq += 1

        lifecycle_states = [
            (OrderStatus.COLLECTED, PrepStage.READY, True),
            (OrderStatus.COLLECTED, PrepStage.READY, True),
            (OrderStatus.COLLECTED, PrepStage.READY, False),
            (OrderStatus.PAID, PrepStage.RECEIVED, False),
            (OrderStatus.PAID, PrepStage.IN_PREP, False),
            (OrderStatus.PAID, PrepStage.READY, False),
            (OrderStatus.PENDING_REVIEW, PrepStage.IN_PREP, False),
            (OrderStatus.CREATED, PrepStage.RECEIVED, False),
            (OrderStatus.CANCELLED, PrepStage.RECEIVED, False),
            (OrderStatus.REFUNDED, PrepStage.RECEIVED, False),
        ]

        while seq <= 50:
            owner = clean_users[(seq - 1) % len(clean_users)]
            st, stage, verified = lifecycle_states[(seq - 1) % len(lifecycle_states)]
            if seq <= 32:
                ts = surge_base_utc + timedelta(minutes=(seq % 30))
            else:
                ts = current_time - timedelta(minutes=(seq * 3))
            order_num = f"GC-SEED-{1000 + seq}"
            order_specs.append((order_num, owner, st, stage, ts, verified))
            seq += 1

        inserted_orders = 0
        for idx, (order_num, owner, st, stage, ts, verified) in enumerate(order_specs):
            menu_item = menu_items[idx % len(menu_items)]
            order_id = uuid.uuid4()
            paid_at = ts if st != OrderStatus.CREATED else None
            in_prep_at = ts + timedelta(minutes=2) if stage in (PrepStage.IN_PREP, PrepStage.READY) else None
            ready_at = ts + timedelta(minutes=6) if stage == PrepStage.READY else None
            collected_at = ts + timedelta(minutes=8) if st == OrderStatus.COLLECTED else None
            refunded_at = ts + timedelta(minutes=3) if st == OrderStatus.REFUNDED else None
            cancelled_at = ts + timedelta(minutes=3) if st == OrderStatus.CANCELLED else None

            insert_order = (
                dialect_insert(Order)
                .values(
                    id=order_id,
                    order_number=order_num,
                    user_id=owner.id,
                    restaurant_id=menu_item.restaurant_id,
                    total_paise=menu_item.price_paise,
                    cash_payable_paise=menu_item.price_paise,
                    refund_coins_credited=(
                        menu_item.price_paise // settings.PAISE_PER_GOLD_COIN
                        if st == OrderStatus.REFUNDED
                        else 0
                    ),
                    status=st,
                    prep_stage=stage,
                    qr_nonce=str(uuid.uuid4()),
                    qr_expires_at=ts + timedelta(seconds=settings.QR_EXPIRY_SECONDS),
                    qr_redeemed_at=collected_at,
                    verified_via_scan=verified,
                    paid_at=paid_at,
                    in_prep_at=in_prep_at,
                    ready_at=ready_at,
                    collected_at=collected_at,
                    refunded_at=refunded_at,
                    cancelled_at=cancelled_at,
                    created_at=ts,
                    updated_at=ts,
                )
                .on_conflict_do_nothing(index_elements=["order_number"])
                .returning(Order.id)
            )
            created_id = (await db.execute(insert_order)).scalar_one_or_none()
            if created_id is None:
                continue

            inserted_orders += 1
            db.add(
                OrderItem(
                    id=uuid.uuid4(),
                    order_id=created_id,
                    menu_item_id=menu_item.id,
                    item_name_snapshot=menu_item.name,
                    quantity=1,
                    unit_price_paise=menu_item.price_paise,
                )
            )

            if st in (OrderStatus.REFUNDED, OrderStatus.PENDING_REVIEW):
                is_pending = st == OrderStatus.PENDING_REVIEW
                db.add(
                    Refund(
                        id=uuid.uuid4(),
                        order_id=created_id,
                        user_id=owner.id,
                        restaurant_id=menu_item.restaurant_id,
                        prep_stage_at_request=stage,
                        refund_count_7d=1,
                        surge_active=True,
                        risk_score=0.45 if is_pending else 0.2,
                        factor_breakdown={
                            "s_prep_norm": 0.5 if stage == PrepStage.IN_PREP else 0.0,
                            "v_user_norm": 0.25,
                            "l_surge": 1.0,
                        },
                        decision=RefundDecision.ESCALATE if is_pending else RefundDecision.AUTO_APPROVE,
                        status=RefundStatus.PENDING_REVIEW if is_pending else RefundStatus.COMPLETED,
                        reason_code="RISK_MEDIUM" if is_pending else "RISK_LOW_PRE_PREP",
                        human_message="Seeded refund record",
                        created_at=ts + timedelta(minutes=1),
                        resolved_at=None if is_pending else ts + timedelta(minutes=3),
                    )
                )

            if st == OrderStatus.COLLECTED:
                db.add(
                    AuditLog(
                        id=uuid.uuid4(),
                        order_id=created_id,
                        actor_id=owner.id,
                        restaurant_id=menu_item.restaurant_id,
                        tier=AuditTier.TIER_1 if verified else AuditTier.HUMAN_OVERRIDE,
                        event_type=AuditEvent.QR_VERIFY if verified else AuditEvent.MANUAL_COLLECT,
                        input_state={"seeded": True, "order_number": order_num},
                        risk_score=None,
                        decision="GREEN" if verified else "MANUAL_COLLECT",
                        reason_code="PICKUP_VERIFIED" if verified else "MANUAL_OVERRIDE",
                        human_message=f"Seeded collection for {order_num}",
                        latency_ms=8.5,
                        created_at=ts + timedelta(minutes=8),
                    )
                )
            elif st in (OrderStatus.REFUNDED, OrderStatus.PENDING_REVIEW):
                db.add(
                    AuditLog(
                        id=uuid.uuid4(),
                        order_id=created_id,
                        actor_id=owner.id,
                        restaurant_id=menu_item.restaurant_id,
                        tier=AuditTier.TIER_2,
                        event_type=AuditEvent.REFUND_EVAL,
                        input_state={"seeded": True, "order_number": order_num},
                        risk_score=0.45 if st == OrderStatus.PENDING_REVIEW else 0.2,
                        decision="ESCALATE" if st == OrderStatus.PENDING_REVIEW else "AUTO_APPROVE",
                        reason_code="RISK_MEDIUM" if st == OrderStatus.PENDING_REVIEW else "RISK_LOW_PRE_PREP",
                        human_message=f"Seeded refund evaluation for {order_num}",
                        latency_ms=12.4,
                        created_at=ts + timedelta(minutes=2),
                    )
                )

        south_items = list(
            (
                await db.execute(
                    select(MenuItem)
                    .where(MenuItem.restaurant_id == _SOUTH_RESTAURANT_ID)
                    .order_by(MenuItem.name.asc())
                )
            ).scalars().all()
        )
        fresh_items = list(
            (
                await db.execute(
                    select(MenuItem)
                    .where(MenuItem.restaurant_id == _FRESH_RESTAURANT_ID)
                    .order_by(MenuItem.name.asc())
                )
            ).scalars().all()
        )
        for prefix, r_items in (("S", south_items), ("F", fresh_items)):
            for idx, m_item in enumerate(r_items[:4], start=1):
                o_num = f"GC-SEED-{prefix}{idx:02d}"
                o_owner = clean_users[idx % len(clean_users)]
                o_status = OrderStatus.COLLECTED if idx == 1 else OrderStatus.PAID
                o_stage = PrepStage.READY if idx == 1 else PrepStage.RECEIVED
                o_ts = current_time - timedelta(minutes=idx * 5)
                c_id = (
                    await db.execute(
                        dialect_insert(Order)
                        .values(
                            id=uuid.uuid4(),
                            order_number=o_num,
                            user_id=o_owner.id,
                            restaurant_id=m_item.restaurant_id,
                            total_paise=m_item.price_paise,
                            cash_payable_paise=m_item.price_paise,
                            status=o_status,
                            prep_stage=o_stage,
                            qr_nonce=str(uuid.uuid4()),
                            qr_expires_at=o_ts + timedelta(seconds=settings.QR_EXPIRY_SECONDS),
                            verified_via_scan=(o_status == OrderStatus.COLLECTED),
                            paid_at=o_ts,
                            collected_at=(o_ts + timedelta(minutes=3)) if o_status == OrderStatus.COLLECTED else None,
                            created_at=o_ts,
                            updated_at=o_ts,
                        )
                        .on_conflict_do_nothing(index_elements=["order_number"])
                        .returning(Order.id)
                    )
                ).scalar_one_or_none()
                if c_id is not None:
                    inserted_orders += 1
                    db.add(
                        OrderItem(
                            id=uuid.uuid4(),
                            order_id=c_id,
                            menu_item_id=m_item.id,
                            item_name_snapshot=m_item.name,
                            quantity=1,
                            unit_price_paise=m_item.price_paise,
                            original_unit_price_paise=m_item.price_paise,
                            discount_percent_applied=0,
                        )
                    )

        for idx, stu in enumerate(clean_users):
            is_primary = stu.email == "student01@campus.edu"
            pts = 85 if is_primary else 25
            coins = 120 if is_primary else 40
            life_pts = 105 if is_primary else 25
            life_coins = 122 if is_primary else 40
            await db.execute(
                dialect_insert(StudentWallet)
                .values(
                    user_id=stu.id,
                    loyalty_points=pts,
                    gold_coins=coins,
                    lifetime_points_earned=life_pts,
                    lifetime_coins_earned=life_coins,
                    updated_at=current_time,
                )
                .on_conflict_do_nothing(index_elements=["user_id"])
            )
            if is_primary:
                seed_txs = [
                    (
                        "seed:stu01:earn",
                        WalletTransactionType.POINTS_EARNED_PICKUP,
                        105,
                        0,
                        105,
                        0,
                        "Earned 105 Loyalty Points from campus canteen pickups",
                        current_time - timedelta(days=2),
                    ),
                    (
                        "seed:stu01:convert",
                        WalletTransactionType.POINTS_CONVERTED_TO_COINS,
                        -20,
                        2,
                        85,
                        2,
                        "Converted 20 Loyalty Points into 2 Gold Coins",
                        current_time - timedelta(days=1, hours=4),
                    ),
                    (
                        "seed:stu01:refund",
                        WalletTransactionType.REFUND_CREDITED_COINS,
                        0,
                        118,
                        85,
                        120,
                        "Instant refund of 118 Gold Coins for pre-prep cancellation",
                        current_time - timedelta(hours=6),
                    ),
                ]
                for idem, tx_type, p_d, c_d, bp, bc, desc_text, tx_ts in seed_txs:
                    await db.execute(
                        dialect_insert(WalletTransaction)
                        .values(
                            id=uuid.uuid4(),
                            user_id=stu.id,
                            order_id=None,
                            tx_type=tx_type,
                            points_delta=p_d,
                            coins_delta=c_d,
                            balance_points_after=bp,
                            balance_coins_after=bc,
                            idempotency_key=idem,
                            description=desc_text,
                            created_at=tx_ts,
                        )
                        .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    )
            else:
                await db.execute(
                    dialect_insert(WalletTransaction)
                    .values(
                        id=uuid.uuid4(),
                        user_id=stu.id,
                        order_id=None,
                        tx_type=WalletTransactionType.REFUND_CREDITED_COINS,
                        points_delta=0,
                        coins_delta=coins,
                        balance_points_after=pts,
                        balance_coins_after=coins,
                        idempotency_key=f"seed:{stu.email}:init",
                        description=f"Seeded welcome wallet balance ({coins} Gold Coins)",
                        created_at=current_time - timedelta(days=1),
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                )

        for abuser in abuser_users:
            await db.execute(
                dialect_insert(StudentWallet)
                .values(
                    user_id=abuser.id,
                    loyalty_points=0,
                    gold_coins=0,
                    lifetime_points_earned=0,
                    lifetime_coins_earned=0,
                    updated_at=current_time,
                )
                .on_conflict_do_nothing(index_elements=["user_id"])
            )

        await db.commit()
        return {"inserted_orders": inserted_orders}
    finally:
        if owns_session:
            await session_cm.__aexit__(None, None, None)


def main() -> None:
    asyncio.run(seed_database())


if __name__ == "__main__":
    main()
