from datetime import timedelta
import hashlib
import json
import time
import uuid

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse
from sqlalchemy import desc, nullslast, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_roles, user_rate_limit
from app.core.clock import now
from app.core.database import get_db
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.base import AuditEvent, AuditTier, OrderStatus, PrepStage, UserRole
from app.models.order import Order
from app.models.user import User
from app.schemas.common import format_ist_iso, format_optional_timestamps, format_utc_z
from app.schemas.manager import (
    KdsCancelledEntry,
    KdsOrderEntry,
    KdsOrderItem,
    KdsOrdersResponse,
    KdsStageTransitionResponse,
)
from app.services.audit_service import record_decision

router = APIRouter(prefix="/kds", tags=["kds"])


@router.get(
    "/orders",
    response_model=KdsOrdersResponse,
    summary="List active kitchen orders and recently cancelled items with ETag support",
    dependencies=[Depends(user_rate_limit("kds_orders", limit=120, window_seconds=60))],
)
async def get_kds_orders(
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    current_user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    current_time = now()
    active_stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.status.in_([OrderStatus.PAID, OrderStatus.PENDING_REVIEW]))
        .order_by(nullslast(Order.paid_at.asc()), Order.created_at.asc())
    )
    if current_user.restaurant_id is not None:
        active_stmt = active_stmt.where(Order.restaurant_id == current_user.restaurant_id)
    active_orders = list((await db.execute(active_stmt)).scalars().all())

    cutoff = current_time - timedelta(minutes=5)
    cancelled_stmt = (
        select(Order)
        .where(
            Order.status == OrderStatus.REFUNDED,
            Order.refunded_at.is_not(None),
            Order.refunded_at >= cutoff,
        )
        .order_by(desc(Order.refunded_at))
    )
    if current_user.restaurant_id is not None:
        cancelled_stmt = cancelled_stmt.where(Order.restaurant_id == current_user.restaurant_id)
    cancelled_orders = list((await db.execute(cancelled_stmt)).scalars().all())

    etag_source = {
        "active": [
            [
                str(o.id),
                o.status.value,
                o.prep_stage.value,
                o.updated_at.isoformat(),
            ]
            for o in active_orders
        ],
        "cancelled": [
            [
                o.order_number,
                o.prep_stage.value,
                o.refunded_at.isoformat() if o.refunded_at else "",
            ]
            for o in cancelled_orders
        ],
    }
    etag_raw = hashlib.sha256(
        json.dumps(etag_source, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    etag_header = f'"{etag_raw}"'

    if if_none_match:
        normalized_inm = if_none_match.strip().removeprefix("W/").strip().strip('"')
        if normalized_inm == etag_raw:
            return Response(status_code=304, headers={"ETag": etag_header})

    order_entries: list[KdsOrderEntry] = []
    for order in active_orders:
        paid_utc, paid_ist = format_optional_timestamps(order.paid_at)
        ref_time = order.paid_at or order.created_at
        elapsed_seconds = max(0, int((current_time - ref_time).total_seconds()))
        order_entries.append(
            KdsOrderEntry(
                order_id=order.id,
                order_number=order.order_number,
                items=[
                    KdsOrderItem(
                        name=item.item_name_snapshot,
                        quantity=item.quantity,
                    )
                    for item in order.items
                ],
                status=order.status,
                prep_stage=order.prep_stage,
                hold=order.status == OrderStatus.PENDING_REVIEW,
                paid_at=paid_utc,
                paid_at_ist=paid_ist,
                elapsed_seconds=elapsed_seconds,
            )
        )

    cancelled_entries = [
        KdsCancelledEntry(
            order_number=order.order_number,
            prep_stage=order.prep_stage,
            refunded_at=format_utc_z(order.refunded_at),
            refunded_at_ist=format_ist_iso(order.refunded_at),
        )
        for order in cancelled_orders
        if order.refunded_at is not None
    ]

    payload = KdsOrdersResponse(
        orders=order_entries,
        recently_cancelled=cancelled_entries,
        server_time=format_utc_z(current_time),
        server_time_ist=format_ist_iso(current_time),
        etag=etag_header,
    )
    return JSONResponse(
        status_code=200,
        content=payload.model_dump(mode="json"),
        headers={"ETag": etag_header},
    )


@router.post(
    "/orders/{order_id}/start-cooking",
    response_model=KdsStageTransitionResponse,
    summary="Transition a PAID order from RECEIVED to IN_PREP",
    dependencies=[Depends(user_rate_limit("kds_transition", limit=120, window_seconds=60))],
)
async def start_cooking(
    order_id: uuid.UUID,
    current_user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> KdsStageTransitionResponse:
    start = time.perf_counter()
    stmt = select(Order).where(Order.id == order_id).with_for_update()
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found", "ORDER_NOT_FOUND")
    if (
        current_user.restaurant_id is not None
        and order.restaurant_id != current_user.restaurant_id
    ):
        raise ForbiddenError(
            "Order belongs to another restaurant", "FORBIDDEN_RESTAURANT"
        )

    if order.status != OrderStatus.PAID or order.prep_stage != PrepStage.RECEIVED:
        raise ConflictError(
            f"Cannot start cooking for order in status {order.status.value} and stage {order.prep_stage.value}",
            "INVALID_STAGE_TRANSITION",
        )

    current_time = now()
    order.prep_stage = PrepStage.IN_PREP
    order.in_prep_at = current_time
    order.updated_at = current_time

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.TIER_1,
        event_type=AuditEvent.STAGE_CHANGE,
        input_state={
            "order_id": str(order.id),
            "from_stage": PrepStage.RECEIVED.value,
            "to_stage": PrepStage.IN_PREP.value,
        },
        decision=PrepStage.IN_PREP.value,
        reason_code="STAGE_IN_PREP",
        human_message=f"Order {order.order_number} moved to IN_PREP",
        latency_ms=latency_ms,
        order_id=order.id,
        actor_id=current_user.id,
        restaurant_id=order.restaurant_id,
        db=db,
    )
    await db.commit()

    in_prep_utc, in_prep_ist = format_optional_timestamps(order.in_prep_at)
    ready_utc, ready_ist = format_optional_timestamps(order.ready_at)
    return KdsStageTransitionResponse(
        order_id=order.id,
        order_number=order.order_number,
        status=order.status,
        prep_stage=order.prep_stage,
        in_prep_at=in_prep_utc,
        in_prep_at_ist=in_prep_ist,
        ready_at=ready_utc,
        ready_at_ist=ready_ist,
    )


@router.post(
    "/orders/{order_id}/mark-ready",
    response_model=KdsStageTransitionResponse,
    summary="Transition a PAID order from IN_PREP to READY",
    dependencies=[Depends(user_rate_limit("kds_transition", limit=120, window_seconds=60))],
)
async def mark_ready(
    order_id: uuid.UUID,
    current_user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> KdsStageTransitionResponse:
    start = time.perf_counter()
    stmt = select(Order).where(Order.id == order_id).with_for_update()
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found", "ORDER_NOT_FOUND")
    if (
        current_user.restaurant_id is not None
        and order.restaurant_id != current_user.restaurant_id
    ):
        raise ForbiddenError(
            "Order belongs to another restaurant", "FORBIDDEN_RESTAURANT"
        )

    if order.status != OrderStatus.PAID or order.prep_stage != PrepStage.IN_PREP:
        raise ConflictError(
            f"Cannot mark ready for order in status {order.status.value} and stage {order.prep_stage.value}",
            "INVALID_STAGE_TRANSITION",
        )

    current_time = now()
    order.prep_stage = PrepStage.READY
    order.ready_at = current_time
    order.updated_at = current_time

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.TIER_1,
        event_type=AuditEvent.STAGE_CHANGE,
        input_state={
            "order_id": str(order.id),
            "from_stage": PrepStage.IN_PREP.value,
            "to_stage": PrepStage.READY.value,
        },
        decision=PrepStage.READY.value,
        reason_code="STAGE_READY",
        human_message=f"Order {order.order_number} marked READY",
        latency_ms=latency_ms,
        order_id=order.id,
        actor_id=current_user.id,
        restaurant_id=order.restaurant_id,
        db=db,
    )
    await db.commit()

    in_prep_utc, in_prep_ist = format_optional_timestamps(order.in_prep_at)
    ready_utc, ready_ist = format_optional_timestamps(order.ready_at)
    return KdsStageTransitionResponse(
        order_id=order.id,
        order_number=order.order_number,
        status=order.status,
        prep_stage=order.prep_stage,
        in_prep_at=in_prep_utc,
        in_prep_at_ist=in_prep_ist,
        ready_at=ready_utc,
        ready_at_ist=ready_ist,
    )
