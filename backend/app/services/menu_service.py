import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.menu import serialize_menu_item
from app.core.clock import now
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.base import AuditEvent, AuditTier
from app.models.menu import MenuItem
from app.models.restaurant import DEFAULT_RESTAURANT_ID
from app.models.user import User
from app.schemas.menu import (
    MenuItemCreateRequest,
    MenuItemResponse,
    MenuItemUpdateRequest,
)
from app.services.audit_service import record_decision


def _resolve_manager_restaurant_id(current_user: User) -> uuid.UUID:
    return current_user.restaurant_id or DEFAULT_RESTAURANT_ID


async def list_manager_menu_items(
    db: AsyncSession,
    current_user: User,
) -> list[MenuItemResponse]:
    rid = _resolve_manager_restaurant_id(current_user)
    current_time = now()
    rows = (
        await db.execute(
            select(MenuItem)
            .where(
                MenuItem.restaurant_id == rid,
                MenuItem.is_archived.is_(False),
            )
            .order_by(MenuItem.category.asc(), MenuItem.name.asc())
        )
    ).scalars().all()
    return [serialize_menu_item(item, 0, current_time) for item in rows]


async def create_manager_menu_item(
    db: AsyncSession,
    current_user: User,
    payload: MenuItemCreateRequest,
) -> MenuItemResponse:
    start = time.perf_counter()
    rid = _resolve_manager_restaurant_id(current_user)
    normalized_name = payload.name.strip()

    existing = (
        await db.execute(
            select(MenuItem.id).where(
                MenuItem.restaurant_id == rid,
                MenuItem.name == normalized_name,
                MenuItem.is_archived.is_(False),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            f"Menu item '{normalized_name}' already exists in this restaurant",
            "MENU_ITEM_EXISTS",
        )

    current_time = now()
    item = MenuItem(
        id=uuid.uuid4(),
        restaurant_id=rid,
        name=normalized_name,
        description=payload.description.strip(),
        category=payload.category.strip() or "General",
        price_paise=payload.price_paise,
        stock_count=payload.stock_count,
        estimated_prep_minutes=payload.estimated_prep_minutes,
        is_available=payload.is_available,
        is_archived=False,
        updated_at=current_time,
    )
    db.add(item)
    await db.flush()

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.HUMAN_OVERRIDE,
        event_type=AuditEvent.MENU_ITEM_CREATED,
        input_state={
            "item_id": str(item.id),
            "name": item.name,
            "category": item.category,
            "price_paise": item.price_paise,
            "stock_count": item.stock_count,
            "estimated_prep_minutes": item.estimated_prep_minutes,
            "is_available": item.is_available,
        },
        decision="MENU_CREATED",
        reason_code="MENU_ITEM_CREATED",
        human_message=f"Created menu item {item.name}",
        latency_ms=latency_ms,
        order_id=None,
        actor_id=current_user.id,
        restaurant_id=rid,
        db=db,
    )
    await db.commit()
    await db.refresh(item)
    return serialize_menu_item(item, 0, current_time)


async def update_manager_menu_item(
    db: AsyncSession,
    current_user: User,
    item_id: uuid.UUID,
    payload: MenuItemUpdateRequest,
) -> MenuItemResponse:
    start = time.perf_counter()
    stmt = select(MenuItem).where(MenuItem.id == item_id).with_for_update()
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None or item.is_archived:
        raise NotFoundError("Menu item not found", "MENU_ITEM_NOT_FOUND")

    if (
        current_user.restaurant_id is not None
        and item.restaurant_id != current_user.restaurant_id
    ):
        raise ForbiddenError(
            "Cannot modify menu items belonging to another restaurant",
            "FORBIDDEN_RESTAURANT",
        )

    before_state = {
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "price_paise": item.price_paise,
        "stock_count": item.stock_count,
        "estimated_prep_minutes": item.estimated_prep_minutes,
        "is_available": item.is_available,
    }

    if payload.name is not None:
        new_name = payload.name.strip()
        if new_name != item.name:
            dup = (
                await db.execute(
                    select(MenuItem.id).where(
                        MenuItem.restaurant_id == item.restaurant_id,
                        MenuItem.name == new_name,
                        MenuItem.is_archived.is_(False),
                        MenuItem.id != item.id,
                    )
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise ConflictError(
                    f"Menu item '{new_name}' already exists in this restaurant",
                    "MENU_ITEM_EXISTS",
                )
            item.name = new_name
    if payload.description is not None:
        item.description = payload.description.strip()
    if payload.category is not None:
        item.category = payload.category.strip() or "General"
    if payload.price_paise is not None:
        item.price_paise = payload.price_paise
    if payload.stock_count is not None:
        item.stock_count = payload.stock_count
    if payload.estimated_prep_minutes is not None:
        item.estimated_prep_minutes = payload.estimated_prep_minutes
    if payload.is_available is not None:
        item.is_available = payload.is_available

    current_time = now()
    item.updated_at = current_time

    after_state = {
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "price_paise": item.price_paise,
        "stock_count": item.stock_count,
        "estimated_prep_minutes": item.estimated_prep_minutes,
        "is_available": item.is_available,
    }

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.HUMAN_OVERRIDE,
        event_type=AuditEvent.MENU_ITEM_UPDATED,
        input_state={
            "item_id": str(item.id),
            "before": before_state,
            "after": after_state,
        },
        decision="MENU_UPDATED",
        reason_code="MENU_ITEM_UPDATED",
        human_message=f"Updated menu item {item.name}",
        latency_ms=latency_ms,
        order_id=None,
        actor_id=current_user.id,
        restaurant_id=item.restaurant_id,
        db=db,
    )
    await db.commit()
    await db.refresh(item)
    return serialize_menu_item(item, 0, current_time)


async def archive_manager_menu_item(
    db: AsyncSession,
    current_user: User,
    item_id: uuid.UUID,
) -> MenuItemResponse:
    start = time.perf_counter()
    stmt = select(MenuItem).where(MenuItem.id == item_id).with_for_update()
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise NotFoundError("Menu item not found", "MENU_ITEM_NOT_FOUND")

    if (
        current_user.restaurant_id is not None
        and item.restaurant_id != current_user.restaurant_id
    ):
        raise ForbiddenError(
            "Cannot archive menu items belonging to another restaurant",
            "FORBIDDEN_RESTAURANT",
        )

    current_time = now()
    item.is_archived = True
    item.is_available = False
    item.is_surplus_deal = False
    item.discount_percent = 0
    item.surplus_note = None
    item.surplus_expires_at = None
    item.updated_at = current_time

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.HUMAN_OVERRIDE,
        event_type=AuditEvent.MENU_ITEM_ARCHIVED,
        input_state={
            "item_id": str(item.id),
            "name": item.name,
            "category": item.category,
        },
        decision="MENU_ARCHIVED",
        reason_code="MENU_ITEM_ARCHIVED",
        human_message=f"Archived menu item {item.name}",
        latency_ms=latency_ms,
        order_id=None,
        actor_id=current_user.id,
        restaurant_id=item.restaurant_id,
        db=db,
    )
    await db.commit()
    await db.refresh(item)
    return serialize_menu_item(item, 0, current_time)
