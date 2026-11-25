from datetime import timedelta
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_roles, user_rate_limit
from app.api.v1.menu import serialize_menu_item
from app.core.clock import now
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models.base import (
    AuditEvent,
    AuditTier,
    OrderStatus,
    PrepStage,
    RefundDecision,
    RefundStatus,
    UserRole,
)
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.refund import Refund
from app.models.user import User
from app.schemas.common import format_ist_iso, format_optional_timestamps, format_utc_z
from app.schemas.manager import (
    AuditLogListResponse,
    EscalationItemResponse,
    EscalationOrderItem,
    ManagerDecisionRequest,
    ManagerDecisionResponse,
    ManagerMetricsResponse,
)
from app.schemas.menu import (
    MenuItemCreateRequest,
    MenuItemResponse,
    MenuItemUpdateRequest,
    SurplusDealRequest,
)
from app.services.audit_service import record_decision
from app.services.menu_service import (
    archive_manager_menu_item,
    create_manager_menu_item,
    list_manager_menu_items,
    update_manager_menu_item,
)
from app.services.metrics_service import (
    compute_manager_metrics,
    list_audit_logs_paginated,
)
from app.services.stock_service import restore_stock
from app.services.wallet_service import credit_refund_coins

router = APIRouter(prefix="/manager", tags=["manager"])


@router.get(
    "/escalations",
    response_model=list[EscalationItemResponse],
    summary="List pending Tier 2 refund escalations ordered oldest first",
    dependencies=[Depends(user_rate_limit("manager_queue", limit=60, window_seconds=60))],
)
async def list_escalations(
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> list[EscalationItemResponse]:
    current_time = now()
    sla_seconds = settings.ESCALATION_SLA_MINUTES * 60

    stmt = (
        select(Refund, Order, User)
        .join(Order, Order.id == Refund.order_id)
        .join(User, User.id == Refund.user_id)
        .options(selectinload(Order.items))
        .where(Refund.status == RefundStatus.PENDING_REVIEW)
        .order_by(Refund.created_at.asc())
    )
    if current_user.restaurant_id is not None:
        stmt = stmt.where(Order.restaurant_id == current_user.restaurant_id)
    rows = (await db.execute(stmt)).all()

    results: list[EscalationItemResponse] = []
    for refund, order, student in rows:
        age_seconds = max(0, int((current_time - refund.created_at).total_seconds()))
        paid_utc, paid_ist = format_optional_timestamps(order.paid_at)
        in_prep_utc, in_prep_ist = format_optional_timestamps(order.in_prep_at)
        ready_utc, ready_ist = format_optional_timestamps(order.ready_at)

        results.append(
            EscalationItemResponse(
                refund_id=refund.id,
                order_id=order.id,
                order_number=order.order_number,
                total_paise=order.total_paise,
                items=[
                    EscalationOrderItem(
                        name=item.item_name_snapshot,
                        quantity=item.quantity,
                        unit_price_paise=item.unit_price_paise,
                    )
                    for item in order.items
                ],
                student_name=student.name,
                student_email=student.email,
                refund_count_7d=refund.refund_count_7d,
                prep_stage_at_request=refund.prep_stage_at_request,
                risk_score=refund.risk_score,
                factor_breakdown=refund.factor_breakdown,
                reason_code=refund.reason_code,
                human_message=refund.human_message,
                paid_at=paid_utc,
                paid_at_ist=paid_ist,
                in_prep_at=in_prep_utc,
                in_prep_at_ist=in_prep_ist,
                ready_at=ready_utc,
                ready_at_ist=ready_ist,
                created_at=format_utc_z(refund.created_at),
                created_at_ist=format_ist_iso(refund.created_at),
                age_seconds=age_seconds,
                is_overdue=age_seconds > sla_seconds,
            )
        )

    return results


@router.post(
    "/escalations/{refund_id}/decision",
    response_model=ManagerDecisionResponse,
    summary="Approve or reject a pending Tier 2 refund escalation",
    dependencies=[Depends(user_rate_limit("manager_decision", limit=60, window_seconds=60))],
)
async def decide_escalation(
    refund_id: uuid.UUID,
    payload: ManagerDecisionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> ManagerDecisionResponse:
    start = time.perf_counter()

    refund_stmt = select(Refund).where(Refund.id == refund_id).with_for_update()
    refund = (await db.execute(refund_stmt)).scalar_one_or_none()
    if refund is None:
        raise NotFoundError("Refund escalation not found", "REFUND_NOT_FOUND")

    if refund.status != RefundStatus.PENDING_REVIEW:
        raise ConflictError(
            f"Refund escalation already resolved in status {refund.status.value}",
            "INVALID_REFUND_STATUS",
        )

    order_stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == refund.order_id)
        .with_for_update()
    )
    order = (await db.execute(order_stmt)).scalar_one()
    if (
        current_user.restaurant_id is not None
        and order.restaurant_id != current_user.restaurant_id
    ):
        raise ForbiddenError(
            "Cannot decide escalation for another restaurant",
            "FORBIDDEN_RESTAURANT",
        )

    current_time = now()
    refund.manager_id = current_user.id
    refund.manager_notes = payload.notes
    refund.resolved_at = current_time

    coins_credited = 0
    if payload.action == "APPROVE":
        coins_credited = await credit_refund_coins(
            db=db,
            user_id=order.user_id,
            order_id=order.id,
            order_number=order.order_number,
            total_paise=order.total_paise,
        )
        order.refund_coins_credited = coins_credited
        refund.status = RefundStatus.COMPLETED
        refund.decision = RefundDecision.MANAGER_APPROVE
        order.status = OrderStatus.REFUNDED
        order.refunded_at = current_time
        order.updated_at = current_time
        if refund.prep_stage_at_request == PrepStage.RECEIVED:
            await restore_stock(
                db,
                [(item.menu_item_id, item.quantity) for item in order.items],
            )
        reason_code = "MANAGER_APPROVED_REFUND"
        human_message = (
            payload.notes
            or f"Manager approved escalated refund. {coins_credited} Gold Coins credited."
        )
    else:
        refund.status = RefundStatus.REJECTED
        refund.decision = RefundDecision.MANAGER_REJECT
        order.status = OrderStatus.PAID
        order.updated_at = current_time
        reason_code = "MANAGER_REJECTED_REFUND"
        human_message = payload.notes or "Manager rejected escalated refund."

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.HUMAN_OVERRIDE,
        event_type=AuditEvent.MANAGER_DECISION,
        input_state={
            "refund_id": str(refund.id),
            "order_id": str(order.id),
            "action": payload.action,
            "prep_stage_at_request": refund.prep_stage_at_request.value,
            "factor_breakdown": refund.factor_breakdown,
            "coins_credited": coins_credited,
        },
        decision=refund.decision.value,
        reason_code=reason_code,
        human_message=human_message,
        latency_ms=latency_ms,
        order_id=order.id,
        actor_id=current_user.id,
        risk_score=refund.risk_score,
        restaurant_id=order.restaurant_id,
        db=db,
    )
    await db.commit()

    return ManagerDecisionResponse(
        refund_id=refund.id,
        order_id=order.id,
        decision=refund.decision,
        refund_status=refund.status,
        order_status=order.status,
        resolved_at=format_utc_z(current_time),
        resolved_at_ist=format_ist_iso(current_time),
    )


@router.get(
    "/audit-logs",
    response_model=AuditLogListResponse,
    summary="List append-only audit log entries with filtering and cursor pagination",
    dependencies=[Depends(user_rate_limit("manager_audit_logs", limit=60, window_seconds=60))],
)
async def get_audit_logs(
    limit: int = Query(default=20, ge=1, le=100),
    event_type: AuditEvent | None = Query(default=None),
    tier: AuditTier | None = Query(default=None),
    decision: str | None = Query(default=None),
    reason_code: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    current_user: User = Depends(require_roles(UserRole.MANAGER, UserRole.STAFF)),
    db: AsyncSession = Depends(get_db),
) -> AuditLogListResponse:
    return await list_audit_logs_paginated(
        db=db,
        limit=limit,
        event_type=event_type,
        tier=tier,
        decision=decision,
        reason_code=reason_code,
        cursor=cursor,
        restaurant_id=current_user.restaurant_id,
    )


@router.get(
    "/metrics",
    response_model=ManagerMetricsResponse,
    summary="Compute live IST-day operational, ROI, and scan-compliance metrics",
    dependencies=[Depends(user_rate_limit("manager_metrics", limit=60, window_seconds=60))],
)
async def get_metrics(
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> ManagerMetricsResponse:
    return await compute_manager_metrics(db, restaurant_id=current_user.restaurant_id)


@router.get(
    "/menu",
    response_model=list[MenuItemResponse],
    summary="List all non-archived menu items for the manager restaurant",
    dependencies=[Depends(user_rate_limit("manager_menu_read", limit=60, window_seconds=60))],
)
async def list_manager_menu(
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> list[MenuItemResponse]:
    return await list_manager_menu_items(db=db, current_user=current_user)


@router.post(
    "/menu",
    response_model=MenuItemResponse,
    status_code=201,
    summary="Create a new menu item for the manager restaurant",
    dependencies=[Depends(user_rate_limit("manager_menu_write", limit=60, window_seconds=60))],
)
async def create_manager_menu(
    payload: MenuItemCreateRequest,
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> MenuItemResponse:
    return await create_manager_menu_item(db=db, current_user=current_user, payload=payload)


@router.patch(
    "/menu/{item_id}",
    response_model=MenuItemResponse,
    summary="Update a menu item owned by the manager restaurant",
    dependencies=[Depends(user_rate_limit("manager_menu_write", limit=60, window_seconds=60))],
)
async def patch_manager_menu(
    item_id: uuid.UUID,
    payload: MenuItemUpdateRequest,
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> MenuItemResponse:
    return await update_manager_menu_item(
        db=db, current_user=current_user, item_id=item_id, payload=payload
    )


@router.delete(
    "/menu/{item_id}",
    response_model=MenuItemResponse,
    summary="Soft-archive a menu item owned by the manager restaurant",
    dependencies=[Depends(user_rate_limit("manager_menu_write", limit=60, window_seconds=60))],
)
async def delete_manager_menu(
    item_id: uuid.UUID,
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> MenuItemResponse:
    return await archive_manager_menu_item(
        db=db, current_user=current_user, item_id=item_id
    )


@router.get(
    "/menu/surplus-overview",
    response_model=list[MenuItemResponse],
    summary="List menu items ordered by stock_count DESC for surplus discount management",
    dependencies=[Depends(user_rate_limit("manager_surplus_read", limit=60, window_seconds=60))],
)
async def get_surplus_overview(
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> list[MenuItemResponse]:
    current_time = now()
    stmt = (
        select(MenuItem)
        .where(MenuItem.is_archived.is_(False))
        .order_by(MenuItem.stock_count.desc(), MenuItem.name.asc())
    )
    if current_user.restaurant_id is not None:
        stmt = stmt.where(MenuItem.restaurant_id == current_user.restaurant_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_menu_item(item, 0, current_time) for item in rows]


@router.post(
    "/menu/{item_id}/surplus-deal",
    response_model=MenuItemResponse,
    summary="Activate, update, or deactivate a surplus food discount on a menu item",
    dependencies=[Depends(user_rate_limit("manager_surplus_write", limit=60, window_seconds=60))],
)
async def update_surplus_deal(
    item_id: uuid.UUID,
    payload: SurplusDealRequest,
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> MenuItemResponse:
    start = time.perf_counter()
    if payload.is_surplus_deal and not (5 <= payload.discount_percent <= 90):
        raise BadRequestError(
            "discount_percent must be between 5 and 90 when surplus deal is enabled",
            "INVALID_DISCOUNT_PERCENT",
        )

    stmt = select(MenuItem).where(MenuItem.id == item_id).with_for_update()
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None or item.is_archived:
        raise NotFoundError("Menu item not found", "MENU_ITEM_NOT_FOUND")
    if (
        current_user.restaurant_id is not None
        and item.restaurant_id != current_user.restaurant_id
    ):
        raise ForbiddenError(
            "Cannot modify surplus deal for another restaurant",
            "FORBIDDEN_RESTAURANT",
        )

    current_time = now()
    old_discount_percent = item.discount_percent

    if payload.is_surplus_deal:
        item.is_surplus_deal = True
        item.discount_percent = payload.discount_percent
        item.surplus_note = payload.surplus_note.strip() if payload.surplus_note else None
        item.surplus_expires_at = (
            current_time + timedelta(minutes=payload.expires_in_minutes)
            if payload.expires_in_minutes is not None
            else None
        )
    else:
        item.is_surplus_deal = False
        item.discount_percent = 0
        item.surplus_note = None
        item.surplus_expires_at = None

    item.updated_at = current_time

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    expires_utc = (
        format_utc_z(item.surplus_expires_at)
        if item.surplus_expires_at is not None
        else None
    )
    decision_str = "SURPLUS_ENABLED" if item.is_surplus_deal else "SURPLUS_DISABLED"
    human_msg = (
        f"Activated {item.discount_percent}% surplus discount on {item.name}"
        if item.is_surplus_deal
        else f"Ended surplus discount on {item.name}"
    )

    await record_decision(
        tier=AuditTier.HUMAN_OVERRIDE,
        event_type=AuditEvent.SURPLUS_DISCOUNT_UPDATE,
        input_state={
            "item_id": str(item.id),
            "item_name": item.name,
            "stock_count": item.stock_count,
            "old_discount_percent": old_discount_percent,
            "new_discount_percent": item.discount_percent,
            "surplus_expires_at": expires_utc,
        },
        decision=decision_str,
        reason_code="SURPLUS_DEAL_UPDATED",
        human_message=human_msg,
        latency_ms=latency_ms,
        order_id=None,
        actor_id=current_user.id,
        restaurant_id=item.restaurant_id,
        db=db,
    )
    await db.commit()
    await db.refresh(item)
    return serialize_menu_item(item, 0, current_time)
