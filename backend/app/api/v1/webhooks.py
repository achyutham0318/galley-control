import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.database import get_db
from app.core.errors import BadRequestError
from app.models.base import AuditEvent, AuditTier, OrderStatus, RefundStatus
from app.models.order import Order
from app.models.refund import Refund
from app.models.webhook_event import WebhookEvent
from app.services.audit_service import record_decision
from app.services.razorpay_service import verify_webhook_signature

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/razorpay",
    summary="Reconcile Razorpay payment and refund webhooks idempotently",
)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(
        default=None, alias="X-Razorpay-Signature"
    ),
    x_razorpay_event_id: str | None = Header(
        default=None, alias="X-Razorpay-Event-Id"
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    raw_body = await request.body()
    if not x_razorpay_signature or not verify_webhook_signature(
        raw_body, x_razorpay_signature
    ):
        raise BadRequestError(
            "Invalid Razorpay webhook signature", "INVALID_WEBHOOK_SIGNATURE"
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise BadRequestError("Malformed webhook JSON payload", "INVALID_JSON") from exc

    event_type = str(payload.get("event", "unknown"))
    provider_event_id = (
        x_razorpay_event_id
        or payload.get("event_id")
        or payload.get("id")
        or hashlib.sha256(raw_body).hexdigest()
    )
    provider_event_id = str(provider_event_id)
    current_time = now()

    try:
        async with db.begin_nested():
            db.add(
                WebhookEvent(
                    id=uuid.uuid4(),
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                    processed_at=current_time,
                )
            )
            await db.flush()
    except IntegrityError:
        return {"status": "duplicate", "event_id": provider_event_id}

    event_payload = payload.get("payload", {})

    if event_type == "payment.captured":
        payment_entity = event_payload.get("payment", {}).get("entity", {})
        rzp_order_id = payment_entity.get("order_id")
        rzp_payment_id = payment_entity.get("id")
        captured_amount = payment_entity.get("amount")

        if rzp_order_id:
            order_stmt = (
                select(Order)
                .where(Order.razorpay_order_id == str(rzp_order_id))
                .with_for_update()
            )
            order = (await db.execute(order_stmt)).scalar_one_or_none()
            if order is not None and order.status == OrderStatus.CREATED:
                if captured_amount == order.total_paise:
                    order.status = OrderStatus.PAID
                    order.paid_at = current_time
                    order.razorpay_payment_id = (
                        str(rzp_payment_id) if rzp_payment_id else None
                    )
                    order.updated_at = current_time
                    await record_decision(
                        tier=AuditTier.TIER_1,
                        event_type=AuditEvent.PAYMENT_EVENT,
                        input_state={
                            "provider_event_id": provider_event_id,
                            "razorpay_order_id": str(rzp_order_id),
                            "captured_paise": captured_amount,
                        },
                        decision="PAID",
                        reason_code="WEBHOOK_PAYMENT_CAPTURED",
                        human_message=f"Webhook captured payment for {order.order_number}",
                        latency_ms=0.0,
                        order_id=order.id,
                        actor_id=None,
                        db=db,
                    )
                else:
                    await record_decision(
                        tier=AuditTier.TIER_1,
                        event_type=AuditEvent.PAYMENT_EVENT,
                        input_state={
                            "provider_event_id": provider_event_id,
                            "razorpay_order_id": str(rzp_order_id),
                            "expected_paise": order.total_paise,
                            "captured_paise": captured_amount,
                        },
                        decision="AMOUNT_MISMATCH",
                        reason_code="PAYMENT_AMOUNT_MISMATCH",
                        human_message=(
                            f"Webhook amount mismatch for {order.order_number}: "
                            f"expected {order.total_paise}, got {captured_amount}"
                        ),
                        latency_ms=0.0,
                        order_id=order.id,
                        actor_id=None,
                        db=db,
                    )

    elif event_type == "refund.processed":
        refund_entity = event_payload.get("refund", {}).get("entity", {})
        rzp_refund_id = refund_entity.get("id")
        if rzp_refund_id:
            refund_stmt = (
                select(Refund)
                .where(Refund.razorpay_refund_id == str(rzp_refund_id))
                .with_for_update()
            )
            refund = (await db.execute(refund_stmt)).scalar_one_or_none()
            if refund is not None and refund.status != RefundStatus.COMPLETED:
                refund.status = RefundStatus.COMPLETED
                if refund.resolved_at is None:
                    refund.resolved_at = current_time
                await record_decision(
                    tier=AuditTier.TIER_1,
                    event_type=AuditEvent.PAYMENT_EVENT,
                    input_state={
                        "provider_event_id": provider_event_id,
                        "razorpay_refund_id": str(rzp_refund_id),
                    },
                    decision="COMPLETED",
                    reason_code="WEBHOOK_REFUND_PROCESSED",
                    human_message="Razorpay refund processed",
                    latency_ms=0.0,
                    order_id=refund.order_id,
                    actor_id=None,
                    db=db,
                )

    elif event_type == "refund.failed":
        refund_entity = event_payload.get("refund", {}).get("entity", {})
        rzp_refund_id = refund_entity.get("id")
        if rzp_refund_id:
            refund_stmt = (
                select(Refund)
                .where(Refund.razorpay_refund_id == str(rzp_refund_id))
                .with_for_update()
            )
            refund = (await db.execute(refund_stmt)).scalar_one_or_none()
            if refund is not None:
                refund.status = RefundStatus.FAILED
                await record_decision(
                    tier=AuditTier.TIER_1,
                    event_type=AuditEvent.PAYMENT_EVENT,
                    input_state={
                        "provider_event_id": provider_event_id,
                        "razorpay_refund_id": str(rzp_refund_id),
                    },
                    decision="FAILED",
                    reason_code="WEBHOOK_REFUND_FAILED",
                    human_message="Razorpay refund failed and requires manager attention",
                    latency_ms=0.0,
                    order_id=refund.order_id,
                    actor_id=None,
                    db=db,
                )

    await db.commit()
    return {"status": "processed", "event_id": provider_event_id}
