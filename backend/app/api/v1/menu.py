import math
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.config import settings
from app.core.database import get_db
from app.models.menu import MenuItem
from app.models.restaurant import DEFAULT_RESTAURANT_ID, Restaurant
from app.schemas.common import format_optional_timestamps
from app.schemas.menu import MenuItemResponse
from app.services.order_service import compute_effective_unit_price

router = APIRouter(prefix="/menu", tags=["menu"])


def serialize_menu_item(
    item: MenuItem,
    current_wait_minutes: int,
    current_time=None,
) -> MenuItemResponse:
    eff_price, disc_pct, is_active = compute_effective_unit_price(item, current_time)
    exp_utc, exp_ist = format_optional_timestamps(item.surplus_expires_at)
    return MenuItemResponse(
        id=item.id,
        restaurant_id=item.restaurant_id,
        name=item.name,
        description=item.description,
        category=item.category,
        price_paise=item.price_paise,
        effective_price_paise=eff_price,
        active_discount_percent=disc_pct,
        is_surplus_active=is_active,
        surplus_note=item.surplus_note if is_active else None,
        surplus_expires_at=exp_utc if is_active else None,
        surplus_expires_at_ist=exp_ist if is_active else None,
        stock_count=item.stock_count,
        is_bulk_excess=item.stock_count >= 15,
        is_available=item.is_available,
        is_archived=item.is_archived,
        current_wait_minutes=current_wait_minutes,
    )


async def compute_restaurant_wait_minutes(
    db: AsyncSession, restaurant_id: uuid.UUID | None = None
) -> int:
    if restaurant_id is not None:
        wait_query = text(
            "SELECT COALESCE(SUM(order_max), 0) FROM ("
            "SELECT MAX(mi.estimated_prep_minutes) AS order_max "
            "FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.id "
            "JOIN menu_items mi ON mi.id = oi.menu_item_id "
            "WHERE o.restaurant_id = :rid AND o.status = 'PAID' AND o.prep_stage IN ('RECEIVED', 'IN_PREP') "
            "GROUP BY o.id"
            ") sub"
        )
        total_prep_minutes = int(
            (await db.execute(wait_query, {"rid": str(restaurant_id)})).scalar_one() or 0
        )
    else:
        wait_query = text(
            "SELECT COALESCE(SUM(order_max), 0) FROM ("
            "SELECT MAX(mi.estimated_prep_minutes) AS order_max "
            "FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.id "
            "JOIN menu_items mi ON mi.id = oi.menu_item_id "
            "WHERE o.status = 'PAID' AND o.prep_stage IN ('RECEIVED', 'IN_PREP') "
            "GROUP BY o.id"
            ") sub"
        )
        total_prep_minutes = int((await db.execute(wait_query)).scalar_one() or 0)

    return max(0, math.ceil(total_prep_minutes / settings.KITCHEN_PARALLELISM))


@router.get(
    "",
    response_model=list[MenuItemResponse],
    summary="List live canteen menu items with current kitchen wait estimate",
)
async def list_menu(
    restaurant_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[MenuItemResponse]:
    target_rid = restaurant_id
    if target_rid is None:
        primary = (
            await db.execute(
                select(Restaurant.id)
                .where(Restaurant.is_active.is_(True))
                .order_by(Restaurant.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        target_rid = primary if primary is not None else DEFAULT_RESTAURANT_ID

    current_wait_minutes = await compute_restaurant_wait_minutes(db, target_rid)
    current_time = now()

    stmt = (
        select(MenuItem)
        .where(
            MenuItem.is_archived.is_(False),
            MenuItem.restaurant_id == target_rid,
        )
        .order_by(MenuItem.name.asc())
    )
    menu_items = (await db.execute(stmt)).scalars().all()
    serialized = [
        serialize_menu_item(item, current_wait_minutes, current_time)
        for item in menu_items
    ]
    serialized.sort(key=lambda item: (not item.is_surplus_active, item.name))
    return serialized
