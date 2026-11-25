import asyncio
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.core.clock import now
from app.core.config import settings
from app.core.database import async_session_maker
from app.models.base import AuditEvent, AuditTier, OrderStatus, RefundStatus
from app.models.order import Order
from app.models.refund import Refund
from app.services.audit_service import record_decision
from app.services.razorpay_service import initiate_refund_async
from app.services.stock_service import restore_stock

_SWEEP_ADVISORY_LOCK_KEY = 7429101
_SWEEP_INTERVAL_SECONDS = 60


async def run_sweep_once() -> dict[str, int]:
    retried_refunds = 0
    cancelled_orders = 0

    async with async_session_maker() as db:
        acquired = bool(
            (
                await db.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _SWEEP_ADVISORY_LOCK_KEY},
                )
            ).scalar_one()
        )
        if not acquired:
            return {"retried_refunds": 0, "cancelled_orders": 0}

        try:
            current_time = now()
            refund_cutoff = current_time - timedelta(minutes=2)
            stuck_refund_ids = list(
                (
                    await db.execute(
                        select(Refund.id).where(
                            Refund.status == RefundStatus.INITIATED,
                            Refund.razorpay_refund_id.is_(None),
                            Refund.created_at <= refund_cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )

            stale_cutoff = current_time - timedelta(
                minutes=settings.ORDER_PAYMENT_TIMEOUT_MINUTES
            )
            stale_stmt = (
                select(Order)
                .options(selectinload(Order.items))
                .where(
                    Order.status == OrderStatus.CREATED,
                    Order.created_at <= stale_cutoff,
                )
                .with_for_update(skip_locked=True)
            )
            stale_orders = list((await db.execute(stale_stmt)).scalars().all())

            for order in stale_orders:
                order.status = OrderStatus.CANCELLED
                order.cancelled_at = current_time
                order.updated_at = current_time
                await restore_stock(
                    db,
                    [(item.menu_item_id, item.quantity) for item in order.items],
                )
                await record_decision(
                    tier=AuditTier.TIER_1,
                    event_type=AuditEvent.PAYMENT_EVENT,
                    input_state={
                        "order_id": str(order.id),
                        "order_number": order.order_number,
                    },
                    decision="CANCELLED",
                    reason_code="STALE_ORDER_CANCELLED",
                    human_message=f"Cancelled stale unpaid order {order.order_number} and restored stock",
                    latency_ms=0.0,
                    order_id=order.id,
                    actor_id=None,
                    db=db,
                )
                cancelled_orders += 1

            await db.commit()

            for refund_id in stuck_refund_ids:
                try:
                    await initiate_refund_async(refund_id)
                    retried_refunds += 1
                except Exception:
                    pass
        finally:
            await db.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": _SWEEP_ADVISORY_LOCK_KEY},
            )

    return {
        "retried_refunds": retried_refunds,
        "cancelled_orders": cancelled_orders,
    }


async def _sweep_loop() -> None:
    while True:
        try:
            await run_sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)


def start_lifecycle_sweep() -> asyncio.Task[None]:
    return asyncio.create_task(_sweep_loop())


async def stop_lifecycle_sweep(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
