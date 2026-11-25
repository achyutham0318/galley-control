import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.errors import ConflictError, NotFoundError
from app.models.menu import MenuItem


async def reserve_stock(
    db: AsyncSession,
    items: list[tuple[uuid.UUID, int]],
) -> dict[uuid.UUID, MenuItem]:
    sorted_items = sorted(items, key=lambda pair: pair[0])
    item_ids = [item_id for item_id, _ in sorted_items]

    stmt = (
        select(MenuItem)
        .where(MenuItem.id.in_(item_ids))
        .order_by(MenuItem.id.asc())
        .with_for_update()
    )
    rows = (await db.execute(stmt)).scalars().all()
    menu_map: dict[uuid.UUID, MenuItem] = {row.id: row for row in rows}

    current_time = now()
    for item_id, quantity in sorted_items:
        menu_item = menu_map.get(item_id)
        if menu_item is None:
            raise NotFoundError(
                message=f"Menu item {item_id} not found",
                code="MENU_ITEM_NOT_FOUND",
            )
        if menu_item.is_archived:
            raise ConflictError(
                message=f"Menu item {menu_item.name} is archived",
                code="ITEM_ARCHIVED",
            )
        if not menu_item.is_available:
            raise ConflictError(
                message=f"Menu item {menu_item.name} is unavailable",
                code="ITEM_UNAVAILABLE",
            )
        if menu_item.stock_count < quantity:
            raise ConflictError(
                message=f"Insufficient stock for {menu_item.name}",
                code="INSUFFICIENT_STOCK",
            )

    for item_id, quantity in sorted_items:
        upd = (
            update(MenuItem)
            .where(
                MenuItem.id == item_id,
                MenuItem.is_available.is_(True),
                MenuItem.stock_count >= quantity,
            )
            .values(
                stock_count=MenuItem.stock_count - quantity,
                updated_at=current_time,
            )
        )
        res = await db.execute(upd)
        if res.rowcount != 1:
            raise ConflictError(
                message=f"Insufficient stock for {menu_map[item_id].name}",
                code="INSUFFICIENT_STOCK",
            )
        db.expire(menu_map[item_id], ["stock_count", "updated_at"])

    return menu_map


async def restore_stock(
    db: AsyncSession,
    items: list[tuple[uuid.UUID, int]],
) -> None:
    if not items:
        return

    sorted_items = sorted(items, key=lambda pair: pair[0])
    item_ids = [item_id for item_id, _ in sorted_items]

    stmt = (
        select(MenuItem)
        .where(MenuItem.id.in_(item_ids))
        .order_by(MenuItem.id.asc())
        .with_for_update()
    )
    rows = (await db.execute(stmt)).scalars().all()
    menu_map: dict[uuid.UUID, MenuItem] = {row.id: row for row in rows}

    current_time = now()
    for item_id, quantity in sorted_items:
        menu_item = menu_map.get(item_id)
        if menu_item is not None:
            upd = (
                update(MenuItem)
                .where(MenuItem.id == item_id)
                .values(
                    stock_count=MenuItem.stock_count + quantity,
                    updated_at=current_time,
                )
            )
            await db.execute(upd)
            db.expire(menu_item, ["stock_count", "updated_at"])
