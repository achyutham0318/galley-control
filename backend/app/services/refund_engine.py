from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
import uuid

from fastapi import BackgroundTasks
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import redis as redis_module
from app.core.clock import is_surge_window, now
from app.core.config import settings
from app.core.errors import ForbiddenError, NotFoundError
from app.core.redis import record_redis_error
from app.models.base import (
    AuditEvent,
    AuditTier,
    OrderStatus,
    PrepStage,
    RefundDecision,
    RefundStatus,
)
from app.models.order import Order
from app.models.refund import Refund
from app.models.user import User
from app.schemas.refund import CancelOrderResponse
from app.services.audit_service import record_decision
from app.services.stock_service import restore_stock
from app.services.wallet_service import credit_refund_coins

_PREP_STAGE_WEIGHT = {
    PrepStage.RECEIVED: 0,
    PrepStage.IN_PREP: 1,
    PrepStage.READY: 2,
}


@dataclass(frozen=True)
class RefundEvaluation:
    risk_score: float
    factor_breakdown: dict[str, float]
    v_user_count: int
    surge_active: bool
    decision: RefundDecision
    reason_code: str


def evaluate(
    order: Order,
    v_user_count: int,
    eval_now: datetime,
) -> RefundEvaluation:
    s_prep_norm = _PREP_STAGE_WEIGHT[order.prep_stage] / 2.0
    v_user_norm = min(v_user_count / 4.0, 1.0)
    surge_active = is_surge_window(eval_now)
    l_surge = 1.0 if surge_active else 0.0

    risk_score = round((0.5 * s_prep_norm) + (0.3 * v_user_norm) + (0.2 * l_surge), 4)
    factor_breakdown = {
        "s_prep_norm": s_prep_norm,
        "v_user_norm": v_user_norm,
        "l_surge": l_surge,
    }

    if risk_score < 0.3:
        if order.prep_stage != PrepStage.RECEIVED:
            decision = RefundDecision.ESCALATE
            reason_code = "STAGE_GUARD_ESCALATION"
        else:
            decision = RefundDecision.AUTO_APPROVE
            reason_code = "RISK_LOW_PRE_PREP"
    elif risk_score < 0.7:
        decision = RefundDecision.ESCALATE
        reason_code = "RISK_MEDIUM"
    else:
        decision = RefundDecision.BLOCK
        reason_code = "RISK_HIGH"

    return RefundEvaluation(
        risk_score=risk_score,
        factor_breakdown=factor_breakdown,
        v_user_count=v_user_count,
        surge_active=surge_active,
        decision=decision,
        reason_code=reason_code,
    )


async def _revoke_nonce_in_redis(order: Order, current_time: datetime) -> None:
    remaining = max(0, int((order.qr_expires_at - current_time).total_seconds()))
    ttl = remaining + settings.QR_REDIS_TTL_BUFFER_SECONDS
    try:
        client = redis_module.get_redis()
        await client.set(f"qr:revoked:{order.qr_nonce}", "1", ex=ttl)
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()


async def cancel_order_with_refund_engine(
    db: AsyncSession,
    order_id: uuid.UUID,
    current_user: User,
    background_tasks: BackgroundTasks,
) -> tuple[int, CancelOrderResponse]:
    start = time.perf_counter()
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
        .with_for_update()
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found", "ORDER_NOT_FOUND")

    if order.user_id != current_user.id:
        raise ForbiddenError("Only the order owner can cancel this order", "FORBIDDEN")

    existing_refund = (
        await db.execute(select(Refund).where(Refund.order_id == order.id))
    ).scalar_one_or_none()

    if existing_refund is not None:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        await record_decision(
            tier=AuditTier.TIER_1,
            event_type=AuditEvent.REFUND_EVAL,
            input_state={
                "order_id": str(order.id),
                "existing_refund_id": str(existing_refund.id),
                "factor_breakdown": existing_refund.factor_breakdown,
            },
            decision=existing_refund.decision.value,
            reason_code="ALREADY_EVALUATED",
            human_message=existing_refund.human_message,
            latency_ms=latency_ms,
            order_id=order.id,
            actor_id=current_user.id,
            risk_score=existing_refund.risk_score,
            db=None,
        )
        return 409, CancelOrderResponse(
            decision=existing_refund.decision.value,
            status=existing_refund.status.value,
            risk_score=existing_refund.risk_score,
            factor_breakdown=existing_refund.factor_breakdown,
            reason_code="ALREADY_EVALUATED",
            human_message=existing_refund.human_message,
            latency_ms=latency_ms,
        )

    current_time = now()

    if order.status == OrderStatus.CREATED:
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = current_time
        order.updated_at = current_time
        await restore_stock(
            db,
            [(item.menu_item_id, item.quantity) for item in order.items],
        )
        human_message = "Unpaid order cancelled and stock restored."
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        await record_decision(
            tier=AuditTier.TIER_1,
            event_type=AuditEvent.REFUND_EVAL,
            input_state={
                "order_id": str(order.id),
                "order_status": OrderStatus.CREATED.value,
            },
            decision="CANCELLED_UNPAID",
            reason_code="UNPAID_ORDER_CANCELLED",
            human_message=human_message,
            latency_ms=latency_ms,
            order_id=order.id,
            actor_id=current_user.id,
            risk_score=None,
            db=db,
        )
        await db.commit()
        return 200, CancelOrderResponse(
            decision="CANCELLED_UNPAID",
            status=OrderStatus.CANCELLED.value,
            risk_score=None,
            factor_breakdown=None,
            reason_code="UNPAID_ORDER_CANCELLED",
            human_message=human_message,
            latency_ms=latency_ms,
        )

    hard_block_map: dict[OrderStatus, tuple[str, str, str, str]] = {
        OrderStatus.COLLECTED: (
            "BLOCK",
            "BLOCKED",
            "ALREADY_COLLECTED",
            "Food already picked up",
        ),
        OrderStatus.REFUNDED: (
            "BLOCK",
            "BLOCKED",
            "ALREADY_REFUNDED",
            "Already refunded",
        ),
        OrderStatus.CANCELLED: (
            "BLOCK",
            "BLOCKED",
            "ALREADY_REFUNDED",
            "Already refunded",
        ),
        OrderStatus.PENDING_REVIEW: (
            "ESCALATE",
            "PENDING_REVIEW",
            "ALREADY_UNDER_REVIEW",
            "Cancellation is already under manager review",
        ),
    }

    if order.status in hard_block_map:
        decision_str, status_str, reason_code, human_message = hard_block_map[order.status]
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        await record_decision(
            tier=AuditTier.TIER_1,
            event_type=AuditEvent.REFUND_EVAL,
            input_state={
                "order_id": str(order.id),
                "order_status": order.status.value,
            },
            decision=decision_str,
            reason_code=reason_code,
            human_message=human_message,
            latency_ms=latency_ms,
            order_id=order.id,
            actor_id=current_user.id,
            risk_score=None,
            db=None,
        )
        return 409, CancelOrderResponse(
            decision=decision_str,
            status=status_str,
            risk_score=None,
            factor_breakdown=None,
            reason_code=reason_code,
            human_message=human_message,
            latency_ms=latency_ms,
        )

    window_start = current_time - timedelta(days=7)
    count_stmt = select(func.count(Refund.id)).where(
        Refund.user_id == order.user_id,
        Refund.created_at >= window_start,
        Refund.order_id != order.id,
    )
    v_user_count = int((await db.execute(count_stmt)).scalar_one() or 0)

    eval_result = evaluate(order=order, v_user_count=v_user_count, eval_now=current_time)

    coins_credited = 0
    if eval_result.decision == RefundDecision.AUTO_APPROVE:
        coins_credited = await credit_refund_coins(
            db=db,
            user_id=order.user_id,
            order_id=order.id,
            order_number=order.order_number,
            total_paise=order.total_paise,
        )
        order.refund_coins_credited = coins_credited
        refund_status = RefundStatus.COMPLETED
        human_message = (
            f"Refund approved. {coins_credited} Gold Coins credited instantly to your Galley Wallet."
        )
        order.status = OrderStatus.REFUNDED
        order.refunded_at = current_time
        order.updated_at = current_time
        await _revoke_nonce_in_redis(order, current_time)
        await restore_stock(
            db,
            [(item.menu_item_id, item.quantity) for item in order.items],
        )
    elif eval_result.decision == RefundDecision.ESCALATE:
        refund_status = RefundStatus.PENDING_REVIEW
        if eval_result.reason_code == "STAGE_GUARD_ESCALATION":
            human_message = (
                f"Risk score {eval_result.risk_score:.2f} requires manager review "
                f"because food preparation has already started ({order.prep_stage.value})."
            )
        else:
            human_message = (
                "Under quick manager review. Expected resolution under 15 minutes."
            )
        order.status = OrderStatus.PENDING_REVIEW
        order.updated_at = current_time
        await _revoke_nonce_in_redis(order, current_time)
    else:
        refund_status = RefundStatus.BLOCKED
        human_message = (
            f"Cannot process refund - order is {order.prep_stage.value} "
            f"with {eval_result.v_user_count} refund requests in the last 7 days."
        )

    refund_row = Refund(
        id=uuid.uuid4(),
        order_id=order.id,
        user_id=order.user_id,
        restaurant_id=order.restaurant_id,
        prep_stage_at_request=order.prep_stage,
        refund_count_7d=eval_result.v_user_count,
        surge_active=eval_result.surge_active,
        risk_score=eval_result.risk_score,
        factor_breakdown=eval_result.factor_breakdown,
        decision=eval_result.decision,
        status=refund_status,
        reason_code=eval_result.reason_code,
        human_message=human_message,
        created_at=current_time,
    )
    db.add(refund_row)

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.TIER_2,
        event_type=AuditEvent.REFUND_EVAL,
        input_state={
            "order_id": str(order.id),
            "prep_stage": order.prep_stage.value,
            "refund_count_7d": eval_result.v_user_count,
            "surge_active": eval_result.surge_active,
            "factor_breakdown": eval_result.factor_breakdown,
        },
        decision=eval_result.decision.value,
        reason_code=eval_result.reason_code,
        human_message=human_message,
        latency_ms=latency_ms,
        order_id=order.id,
        actor_id=current_user.id,
        risk_score=eval_result.risk_score,
        restaurant_id=order.restaurant_id,
        db=db,
    )
    order_uuid = order.id
    actor_uuid = current_user.id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing_after_race = (
            await db.execute(select(Refund).where(Refund.order_id == order_uuid))
        ).scalar_one_or_none()
        if existing_after_race is not None:
            race_latency = round((time.perf_counter() - start) * 1000.0, 3)
            await record_decision(
                tier=AuditTier.TIER_1,
                event_type=AuditEvent.REFUND_EVAL,
                input_state={
                    "order_id": str(order_uuid),
                    "existing_refund_id": str(existing_after_race.id),
                    "factor_breakdown": existing_after_race.factor_breakdown,
                },
                decision=existing_after_race.decision.value,
                reason_code="ALREADY_EVALUATED",
                human_message=existing_after_race.human_message,
                latency_ms=race_latency,
                order_id=order_uuid,
                actor_id=actor_uuid,
                risk_score=existing_after_race.risk_score,
                db=None,
            )
            return 409, CancelOrderResponse(
                decision=existing_after_race.decision.value,
                status=existing_after_race.status.value,
                risk_score=existing_after_race.risk_score,
                factor_breakdown=existing_after_race.factor_breakdown,
                reason_code="ALREADY_EVALUATED",
                human_message=existing_after_race.human_message,
                latency_ms=race_latency,
            )
        raise

    return 200, CancelOrderResponse(
        decision=eval_result.decision.value,
        status=refund_status.value,
        risk_score=eval_result.risk_score,
        factor_breakdown=eval_result.factor_breakdown,
        reason_code=eval_result.reason_code,
        human_message=human_message,
        latency_ms=latency_ms,
        coins_credited=coins_credited,
    )
