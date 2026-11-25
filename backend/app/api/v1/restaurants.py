import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.menu import compute_restaurant_wait_minutes, serialize_menu_item
from app.core.clock import now
from app.core.database import get_db
from app.core.errors import NotFoundError
from app.models.menu import MenuItem
from app.models.restaurant import Restaurant
from app.schemas.menu import MenuItemResponse
from app.schemas.restaurant import RestaurantSummaryResponse
from app.services.order_service import compute_effective_unit_price

router = APIRouter(prefix="/restaurants", tags=["restaurants"])


@router.get(
    "",
    response_model=list[RestaurantSummaryResponse],
    summary="List all active campus restaurants with item counts and live wait times",
)
async def list_restaurants(
    db: AsyncSession = Depends(get_db),
) -> list[RestaurantSummaryResponse]:
    current_time = now()
    restaurants = (
        await db.execute(
            select(Restaurant)
            .where(Restaurant.is_active.is_(True))
            .order_by(Restaurant.created_at.asc(), Restaurant.name.asc())
        )
    ).scalars().all()

    results: list[RestaurantSummaryResponse] = []
    for rest in restaurants:
        items = (
            await db.execute(
                select(MenuItem).where(
                    MenuItem.restaurant_id == rest.id,
                    MenuItem.is_archived.is_(False),
                    MenuItem.is_available.is_(True),
                )
            )
        ).scalars().all()
        active_items_count = len(items)
        active_surplus_deals_count = sum(
            1
            for item in items
            if item.stock_count > 0 and compute_effective_unit_price(item, current_time)[2]
        )
        wait_min = await compute_restaurant_wait_minutes(db, rest.id)
        results.append(
            RestaurantSummaryResponse(
                id=rest.id,
                name=rest.name,
                slug=rest.slug,
                description=rest.description,
                location_tag=rest.location_tag,
                active_items_count=active_items_count,
                active_surplus_deals_count=active_surplus_deals_count,
                current_wait_minutes=wait_min,
            )
        )
    return results


@router.get(
    "/{restaurant_id}/menu",
    response_model=list[MenuItemResponse],
    summary="List available non-archived menu items for a specific restaurant",
)
async def list_restaurant_menu(
    restaurant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[MenuItemResponse]:
    rest = (
        await db.execute(
            select(Restaurant).where(
                Restaurant.id == restaurant_id,
                Restaurant.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if rest is None:
        raise NotFoundError("Restaurant not found", "RESTAURANT_NOT_FOUND")

    current_wait_minutes = await compute_restaurant_wait_minutes(db, rest.id)
    current_time = now()
    items = (
        await db.execute(
            select(MenuItem)
            .where(
                MenuItem.restaurant_id == rest.id,
                MenuItem.is_archived.is_(False),
                MenuItem.is_available.is_(True),
            )
            .order_by(MenuItem.name.asc())
        )
    ).scalars().all()
    serialized = [
        serialize_menu_item(item, current_wait_minutes, current_time)
        for item in items
    ]
    serialized.sort(key=lambda item: (not item.is_surplus_active, item.name))
    return serialized
