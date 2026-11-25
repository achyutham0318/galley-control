import base64
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now, to_ist
from app.core.errors import BadRequestError
from app.core.redis import get_redis_error_metrics
from app.models.audit import AuditLog
from app.models.base import (
    AuditEvent,
    AuditTier,
    OrderStatus,
    RefundDecision,
    RefundStatus,
)
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.refund import Refund
from app.schemas.common import format_ist_human, format_utc_z
from app.schemas.manager import (
    AuditLogEntryResponse,
    AuditLogListResponse,
    ManagerMetricsResponse,
)

_BLOCKED_QR_REASONS = (
    "REPLAY_DETECTED",
    "NONCE_REVOKED",
    "NONCE_SUPERSEDED",
)


def get_ist_day_bounds_utc() -> tuple[datetime, datetime]:
    ist_current = to_ist(now())
    ist_midnight = ist_current.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = ist_midnight.astimezone(timezone.utc)
    day_end_utc = (ist_midnight + timedelta(days=1)).astimezone(timezone.utc)
    return day_start_utc, day_end_utc


def serialize_audit_entry(
    entry: AuditLog,
    order_number: str | None = None,
    total_paise: int | None = None,
) -> AuditLogEntryResponse:
    return AuditLogEntryResponse(
        id=entry.id,
        order_id=entry.order_id,
        order_number=order_number,
        total_paise=total_paise,
        actor_id=entry.actor_id,
        tier=entry.tier,
        event_type=entry.event_type,
        input_state=entry.input_state,
        risk_score=entry.risk_score,
        decision=entry.decision,
        reason_code=entry.reason_code,
        human_message=entry.human_message,
        latency_ms=entry.latency_ms,
        created_at=format_utc_z(entry.created_at),
        created_at_ist=format_ist_human(entry.created_at),
    )


def _encode_audit_cursor(created_at: datetime, entry_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{entry_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_audit_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(f"{cursor}{padding}".encode("ascii")).decode(
            "utf-8"
        )
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except Exception as exc:
        raise BadRequestError("Invalid pagination cursor", "INVALID_CURSOR") from exc


async def list_audit_logs_paginated(
    db: AsyncSession,
    limit: int = 20,
    event_type: AuditEvent | None = None,
    tier: AuditTier | None = None,
    decision: str | None = None,
    reason_code: str | None = None,
    cursor: str | None = None,
    restaurant_id: uuid.UUID | None = None,
) -> AuditLogListResponse:
    stmt = (
        select(AuditLog, Order.order_number, Order.total_paise)
        .outerjoin(Order, Order.id == AuditLog.order_id)
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(limit + 1)
    )

    if restaurant_id is not None:
        stmt = stmt.where(
            or_(
                AuditLog.restaurant_id == restaurant_id,
                Order.restaurant_id == restaurant_id,
            )
        )

    if event_type is not None:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if tier is not None:
        stmt = stmt.where(AuditLog.tier == tier)
    if decision is not None:
        stmt = stmt.where(AuditLog.decision == decision)
    if reason_code is not None:
        stmt = stmt.where(AuditLog.reason_code == reason_code)
    if cursor is not None:
        cursor_ts, cursor_id = _decode_audit_cursor(cursor)
        stmt = stmt.where(
            or_(
                AuditLog.created_at < cursor_ts,
                and_(AuditLog.created_at == cursor_ts, AuditLog.id < cursor_id),
            )
        )

    rows = list((await db.execute(stmt)).all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    next_cursor = None
    if has_more and page_rows:
        last_entry = page_rows[-1][0]
        next_cursor = _encode_audit_cursor(last_entry.created_at, last_entry.id)

    return AuditLogListResponse(
        items=[
            serialize_audit_entry(entry, order_number, total_paise)
            for entry, order_number, total_paise in page_rows
        ],
        next_cursor=next_cursor,
    )


async def compute_manager_metrics(
    db: AsyncSession,
    restaurant_id: uuid.UUID | None = None,
) -> ManagerMetricsResponse:
    day_start, day_end = get_ist_day_bounds_utc()

    order_scope = [Order.restaurant_id == restaurant_id] if restaurant_id is not None else []
    refund_scope = [Refund.restaurant_id == restaurant_id] if restaurant_id is not None else []
    audit_scope = [AuditLog.restaurant_id == restaurant_id] if restaurant_id is not None else []
    menu_scope = [MenuItem.restaurant_id == restaurant_id] if restaurant_id is not None else []

    total_orders_today = int(
        (
            await db.execute(
                select(func.count(Order.id)).where(
                    Order.created_at >= day_start,
                    Order.created_at < day_end,
                    Order.status.notin_([OrderStatus.CREATED, OrderStatus.CANCELLED]),
                    *order_scope,
                )
            )
        ).scalar_one()
        or 0
    )

    gross_revenue_paise = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Order.total_paise), 0)).where(
                    Order.created_at >= day_start,
                    Order.created_at < day_end,
                    Order.status.in_(
                        [
                            OrderStatus.PAID,
                            OrderStatus.COLLECTED,
                            OrderStatus.PENDING_REVIEW,
                        ]
                    ),
                    *order_scope,
                )
            )
        ).scalar_one()
        or 0
    )

    qr_attacks_blocked_count = int(
        (
            await db.execute(
                select(func.count(AuditLog.id)).where(
                    AuditLog.created_at >= day_start,
                    AuditLog.created_at < day_end,
                    AuditLog.event_type == AuditEvent.QR_VERIFY,
                    AuditLog.decision == "RED",
                    AuditLog.reason_code.in_(_BLOCKED_QR_REASONS),
                    *audit_scope,
                )
            )
        ).scalar_one()
        or 0
    )

    blocked_qr_order_ids = select(AuditLog.order_id).where(
        AuditLog.order_id.is_not(None),
        AuditLog.created_at >= day_start,
        AuditLog.created_at < day_end,
        AuditLog.event_type == AuditEvent.QR_VERIFY,
        AuditLog.decision == "RED",
        AuditLog.reason_code.in_(_BLOCKED_QR_REASONS),
        *audit_scope,
    )

    protected_refund_order_ids = select(Refund.order_id).where(
        Refund.created_at >= day_start,
        Refund.created_at < day_end,
        or_(
            Refund.status == RefundStatus.BLOCKED,
            Refund.decision == RefundDecision.MANAGER_REJECT,
        ),
        *refund_scope,
    )

    revenue_protected_paise = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Order.total_paise), 0)).where(
                    or_(
                        Order.id.in_(blocked_qr_order_ids),
                        Order.id.in_(protected_refund_order_ids),
                    ),
                    *order_scope,
                )
            )
        ).scalar_one()
        or 0
    )

    auto_refunds_processed_count = int(
        (
            await db.execute(
                select(func.count(Refund.id)).where(
                    Refund.created_at >= day_start,
                    Refund.created_at < day_end,
                    Refund.decision == RefundDecision.AUTO_APPROVE,
                    *refund_scope,
                )
            )
        ).scalar_one()
        or 0
    )

    pending_escalations_count = int(
        (
            await db.execute(
                select(func.count(Refund.id)).where(
                    Refund.status == RefundStatus.PENDING_REVIEW,
                    *refund_scope,
                )
            )
        ).scalar_one()
        or 0
    )

    avg_qr_verify_latency_ms = round(
        float(
            (
                await db.execute(
                    select(func.coalesce(func.avg(AuditLog.latency_ms), 0.0)).where(
                        AuditLog.created_at >= day_start,
                        AuditLog.created_at < day_end,
                        AuditLog.event_type == AuditEvent.QR_VERIFY,
                        *audit_scope,
                    )
                )
            ).scalar_one()
            or 0.0
        ),
        3,
    )

    compliance_row = (
        await db.execute(
            select(
                func.count(Order.id),
                func.coalesce(
                    func.sum(case((Order.verified_via_scan.is_(True), 1), else_=0)),
                    0,
                ),
            ).where(
                Order.created_at >= day_start,
                Order.created_at < day_end,
                Order.status == OrderStatus.COLLECTED,
                *order_scope,
            )
        )
    ).one()
    collected_total = int(compliance_row[0] or 0)
    verified_total = int(compliance_row[1] or 0)
    scan_compliance_rate = (
        round((verified_total / collected_total) * 100.0, 2)
        if collected_total > 0
        else 100.0
    )

    failed_refunds_count = int(
        (
            await db.execute(
                select(func.count(Refund.id)).where(
                    Refund.created_at >= day_start,
                    Refund.created_at < day_end,
                    Refund.status == RefundStatus.FAILED,
                )
            )
        ).scalar_one()
        or 0
    )

    redis_error_count, redis_error_count_source = await get_redis_error_metrics()

    avg_refund_eval_latency_ms = round(
        float(
            (
                await db.execute(
                    select(func.coalesce(func.avg(AuditLog.latency_ms), 0.0)).where(
                        AuditLog.created_at >= day_start,
                        AuditLog.created_at < day_end,
                        AuditLog.event_type == AuditEvent.REFUND_EVAL,
                    )
                )
            ).scalar_one()
            or 0.0
        ),
        3,
    )

    last_blocked_row = (
        await db.execute(
            select(Order.order_number, AuditLog.created_at)
            .outerjoin(Order, Order.id == AuditLog.order_id)
            .where(
                AuditLog.created_at >= day_start,
                AuditLog.created_at < day_end,
                AuditLog.event_type == AuditEvent.QR_VERIFY,
                AuditLog.decision == "RED",
                AuditLog.reason_code.in_(_BLOCKED_QR_REASONS),
            )
            .order_by(desc(AuditLog.created_at))
            .limit(1)
        )
    ).first()

    last_blocked_order_number = last_blocked_row[0] if last_blocked_row else None
    last_blocked_at = (
        format_utc_z(last_blocked_row[1]) if last_blocked_row else None
    )
    last_blocked_at_ist = (
        format_ist_human(last_blocked_row[1]) if last_blocked_row else None
    )

    current_time = now()
    active_surplus_deals_count = int(
        (
            await db.execute(
                select(func.count(MenuItem.id)).where(
                    MenuItem.is_available.is_(True),
                    MenuItem.is_archived.is_(False),
                    MenuItem.is_surplus_deal.is_(True),
                    MenuItem.discount_percent > 0,
                    MenuItem.stock_count > 0,
                    or_(
                        MenuItem.surplus_expires_at.is_(None),
                        MenuItem.surplus_expires_at > current_time,
                    ),
                    *menu_scope,
                )
            )
        ).scalar_one()
        or 0
    )

    surplus_agg = (
        await db.execute(
            select(
                func.coalesce(func.sum(OrderItem.quantity), 0),
                func.coalesce(func.sum(OrderItem.quantity * OrderItem.unit_price_paise), 0),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.status.in_([OrderStatus.PAID, OrderStatus.COLLECTED]),
                OrderItem.discount_percent_applied > 0,
                *order_scope,
            )
        )
    ).one()
    surplus_units_rescued_count = int(surplus_agg[0] or 0)
    food_waste_prevented_paise = int(surplus_agg[1] or 0)

    return ManagerMetricsResponse(
        total_orders_today=total_orders_today,
        gross_revenue_paise=gross_revenue_paise,
        qr_attacks_blocked_count=qr_attacks_blocked_count,
        revenue_protected_paise=revenue_protected_paise,
        auto_refunds_processed_count=auto_refunds_processed_count,
        pending_escalations_count=pending_escalations_count,
        avg_qr_verify_latency_ms=avg_qr_verify_latency_ms,
        scan_compliance_rate=scan_compliance_rate,
        redis_error_count=redis_error_count,
        redis_error_count_source=redis_error_count_source,
        failed_refunds_count=failed_refunds_count,
        verified_scans_count=verified_total,
        avg_refund_eval_latency_ms=avg_refund_eval_latency_ms,
        last_blocked_order_number=last_blocked_order_number,
        last_blocked_at=last_blocked_at,
        last_blocked_at_ist=last_blocked_at_ist,
        active_surplus_deals_count=active_surplus_deals_count,
        surplus_units_rescued_count=surplus_units_rescued_count,
        food_waste_prevented_paise=food_waste_prevented_paise,
    )
