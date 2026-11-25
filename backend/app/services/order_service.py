import base64
from datetime import datetime, timedelta
import uuid

from sqlalchemy import and_, desc, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.clock import now
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models.base import AuditEvent, AuditTier, OrderStatus, PrepStage, UserRole
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.refund import Refund
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.common import format_ist_iso, format_optional_timestamps, format_utc_z
from app.schemas.order import (
    ConfirmPaymentRequest,
    CreateOrderRequest,
    OrderItemResponse,
    OrderListResponse,
    OrderRefundSummary,
    OrderResponse,
)
from app.schemas.qr import QrRefreshResponse
from app.services.audit_service import record_decision
from app.services.qr_service import sign_qr_payload
from app.services.razorpay_service import create_payment_order, verify_payment_signature
from app.services.stock_service import reserve_stock
from app.services.wallet_service import spend_coins_for_order


def compute_effective_unit_price(
    item: MenuItem,
    current_time: datetime | None = None,
) -> tuple[int, int, bool]:
    check_time = current_time if current_time is not None else now()
    is_active = (
        bool(item.is_surplus_deal)
        and item.discount_percent > 0
        and (item.surplus_expires_at is None or item.surplus_expires_at > check_time)
    )
    if not is_active:
        return item.price_paise, 0, False
    effective = item.price_paise - ((item.price_paise * item.discount_percent) // 100)
    return effective, item.discount_percent, True


def serialize_order(
    order: Order,
    refund: Refund | None = None,
    restaurant_name: str | None = None,
) -> OrderResponse:
    current_time = now()
    qr_token: str | None = None
    qr_seconds_remaining = 0

    if order.status == OrderStatus.PAID:
        exp_unix = int(order.qr_expires_at.timestamp())
        qr_token = sign_qr_payload(order.id, order.qr_nonce, exp_unix)
        qr_seconds_remaining = max(
            0, int((order.qr_expires_at - current_time).total_seconds())
        )

    paid_utc, paid_ist = format_optional_timestamps(order.paid_at)
    in_prep_utc, in_prep_ist = format_optional_timestamps(order.in_prep_at)
    ready_utc, ready_ist = format_optional_timestamps(order.ready_at)
    collected_utc, collected_ist = format_optional_timestamps(order.collected_at)
    refunded_utc, refunded_ist = format_optional_timestamps(order.refunded_at)
    cancelled_utc, cancelled_ist = format_optional_timestamps(order.cancelled_at)

    refund_summary = None
    if refund is not None:
        refund_summary = OrderRefundSummary(
            decision=refund.decision,
            status=refund.status,
            human_message=refund.human_message,
            risk_score=refund.risk_score,
        )

    return OrderResponse(
        id=order.id,
        order_number=order.order_number,
        user_id=order.user_id,
        restaurant_id=order.restaurant_id,
        restaurant_name=restaurant_name,
        total_paise=order.total_paise,
        coins_redeemed=order.coins_redeemed,
        coin_discount_paise=order.coin_discount_paise,
        cash_payable_paise=order.cash_payable_paise,
        refund_coins_credited=order.refund_coins_credited,
        status=order.status,
        prep_stage=order.prep_stage,
        verified_via_scan=order.verified_via_scan,
        qr_token=qr_token,
        qr_expires_at=format_utc_z(order.qr_expires_at),
        qr_expires_at_ist=format_ist_iso(order.qr_expires_at),
        qr_seconds_remaining=qr_seconds_remaining,
        paid_at=paid_utc,
        paid_at_ist=paid_ist,
        in_prep_at=in_prep_utc,
        in_prep_at_ist=in_prep_ist,
        ready_at=ready_utc,
        ready_at_ist=ready_ist,
        collected_at=collected_utc,
        collected_at_ist=collected_ist,
        refunded_at=refunded_utc,
        refunded_at_ist=refunded_ist,
        cancelled_at=cancelled_utc,
        cancelled_at_ist=cancelled_ist,
        created_at=format_utc_z(order.created_at),
        created_at_ist=format_ist_iso(order.created_at),
        updated_at=format_utc_z(order.updated_at),
        updated_at_ist=format_ist_iso(order.updated_at),
        items=[
            OrderItemResponse(
                id=item.id,
                menu_item_id=item.menu_item_id,
                item_name_snapshot=item.item_name_snapshot,
                quantity=item.quantity,
                unit_price_paise=item.unit_price_paise,
                original_unit_price_paise=item.original_unit_price_paise or item.unit_price_paise,
                discount_percent_applied=item.discount_percent_applied,
            )
            for item in order.items
        ],
        refund=refund_summary,
    )


async def _get_order_by_idempotency(
    db: AsyncSession,
    user_id: uuid.UUID,
    idempotency_key: str,
) -> Order | None:
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(
            Order.user_id == user_id,
            Order.idempotency_key == idempotency_key,
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_order(
    db: AsyncSession,
    user: User,
    payload: CreateOrderRequest,
    idempotency_key: str | None = None,
) -> OrderResponse:
    if idempotency_key:
        existing_order = await _get_order_by_idempotency(db, user.id, idempotency_key)
        if existing_order is not None:
            return serialize_order(existing_order)

    requested_pairs = [(item.menu_item_id, item.quantity) for item in payload.items]

    try:
        menu_map = await reserve_stock(db, requested_pairs)
        current_time = now()

        item_rids = {menu_map[item_id].restaurant_id for item_id, _ in requested_pairs}
        if len(item_rids) != 1:
            raise BadRequestError(
                "All items in a single order must belong to the same restaurant",
                "MIXED_RESTAURANT_CART",
            )
        order_restaurant_id = next(iter(item_rids))
        if payload.restaurant_id is not None and payload.restaurant_id != order_restaurant_id:
            raise BadRequestError(
                "Cart items do not match the selected restaurant",
                "MIXED_RESTAURANT_CART",
            )
        rest_row = (
            await db.execute(select(Restaurant).where(Restaurant.id == order_restaurant_id))
        ).scalar_one_or_none()
        if rest_row is not None and not rest_row.is_active:
            raise BadRequestError(
                "Selected restaurant is currently inactive",
                "RESTAURANT_INACTIVE",
            )
        restaurant_name = rest_row.name if rest_row is not None else None

        priced_items: list[tuple[uuid.UUID, int, int, int, int]] = []
        for item_id, quantity in requested_pairs:
            menu_item = menu_map[item_id]
            eff_price, disc_pct, _ = compute_effective_unit_price(menu_item, current_time)
            priced_items.append(
                (item_id, quantity, eff_price, menu_item.price_paise, disc_pct)
            )

        total_paise = sum(eff_price * quantity for _, quantity, eff_price, _, _ in priced_items)

        coins_redeemed = min(
            payload.coins_to_use, total_paise // settings.PAISE_PER_GOLD_COIN
        )
        coin_discount_paise = coins_redeemed * settings.PAISE_PER_GOLD_COIN
        cash_payable_paise = total_paise - coin_discount_paise

        order_id = uuid.uuid4()
        seq_val = (
            await db.execute(text("SELECT nextval('order_number_seq')"))
        ).scalar_one()
        order_number = f"GC-{seq_val}"

        if coins_redeemed > 0:
            await spend_coins_for_order(
                db=db,
                user_id=user.id,
                order_id=order_id,
                order_number=order_number,
                coins_to_spend=coins_redeemed,
            )

        qr_expires_at = current_time + timedelta(seconds=settings.QR_EXPIRY_SECONDS)
        is_mock = settings.RAZORPAY_MOCK_MODE
        instant_paid = is_mock or cash_payable_paise == 0
        order_status = OrderStatus.PAID if instant_paid else OrderStatus.CREATED
        paid_at = current_time if instant_paid else None
        razorpay_order_id: str | None = None
        if not instant_paid:
            razorpay_order_id = await create_payment_order(
                amount_paise=cash_payable_paise,
                receipt=order_number,
            )

        order = Order(
            id=order_id,
            order_number=order_number,
            user_id=user.id,
            restaurant_id=order_restaurant_id,
            total_paise=total_paise,
            coins_redeemed=coins_redeemed,
            coin_discount_paise=coin_discount_paise,
            cash_payable_paise=cash_payable_paise,
            refund_coins_credited=0,
            status=order_status,
            prep_stage=PrepStage.RECEIVED,
            qr_nonce=str(uuid.uuid4()),
            qr_expires_at=qr_expires_at,
            verified_via_scan=False,
            razorpay_order_id=razorpay_order_id,
            idempotency_key=idempotency_key,
            paid_at=paid_at,
            created_at=current_time,
            updated_at=current_time,
        )
        db.add(order)

        order_items = [
            OrderItem(
                id=uuid.uuid4(),
                order_id=order.id,
                menu_item_id=item_id,
                item_name_snapshot=menu_map[item_id].name,
                quantity=quantity,
                unit_price_paise=eff_price,
                original_unit_price_paise=orig_price,
                discount_percent_applied=disc_pct,
            )
            for item_id, quantity, eff_price, orig_price, disc_pct in priced_items
        ]
        db.add_all(order_items)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if idempotency_key:
            existing_order = await _get_order_by_idempotency(
                db, user.id, idempotency_key
            )
            if existing_order is not None:
                return serialize_order(existing_order)
        raise exc

    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order.id)
    )
    created_order = (await db.execute(stmt)).scalar_one()
    return serialize_order(created_order, restaurant_name=restaurant_name)


def _encode_cursor(created_at: datetime, order_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{order_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(f"{cursor}{padding}".encode("ascii")).decode(
            "utf-8"
        )
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except Exception as exc:
        raise BadRequestError("Invalid pagination cursor", "INVALID_CURSOR") from exc


async def list_my_orders(
    db: AsyncSession,
    user_id: uuid.UUID,
    limit: int = 20,
    cursor: str | None = None,
) -> OrderListResponse:
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.user_id == user_id)
        .order_by(desc(Order.created_at), desc(Order.id))
        .limit(limit + 1)
    )

    if cursor is not None:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Order.created_at < cursor_ts,
                and_(Order.created_at == cursor_ts, Order.id < cursor_id),
            )
        )

    orders = list((await db.execute(stmt)).scalars().all())
    has_more = len(orders) > limit
    page_orders = orders[:limit]

    refunds_by_order: dict[uuid.UUID, Refund] = {}
    rest_names: dict[uuid.UUID, str] = {}
    if page_orders:
        order_ids = [order.id for order in page_orders]
        refund_rows = (
            await db.execute(select(Refund).where(Refund.order_id.in_(order_ids)))
        ).scalars().all()
        refunds_by_order = {row.order_id: row for row in refund_rows}
        rids = list({order.restaurant_id for order in page_orders})
        rest_rows = (
            await db.execute(select(Restaurant).where(Restaurant.id.in_(rids)))
        ).scalars().all()
        rest_names = {r.id: r.name for r in rest_rows}

    next_cursor = None
    if has_more and page_orders:
        last_order = page_orders[-1]
        next_cursor = _encode_cursor(last_order.created_at, last_order.id)

    return OrderListResponse(
        items=[
            serialize_order(
                order,
                refunds_by_order.get(order.id),
                restaurant_name=rest_names.get(order.restaurant_id),
            )
            for order in page_orders
        ],
        next_cursor=next_cursor,
    )


async def get_order_detail(
    db: AsyncSession,
    order_id: uuid.UUID,
    current_user: User,
) -> OrderResponse:
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found", "ORDER_NOT_FOUND")

    if current_user.role == UserRole.STUDENT and order.user_id != current_user.id:
        raise ForbiddenError("Cannot access another student's order", "FORBIDDEN")

    refund = (
        await db.execute(select(Refund).where(Refund.order_id == order.id))
    ).scalar_one_or_none()
    rest_name = (
        await db.execute(select(Restaurant.name).where(Restaurant.id == order.restaurant_id))
    ).scalar_one_or_none()

    return serialize_order(order, refund, restaurant_name=rest_name)


async def confirm_order_payment(
    db: AsyncSession,
    order_id: uuid.UUID,
    payload: ConfirmPaymentRequest,
    current_user: User,
) -> QrRefreshResponse:
    stmt = select(Order).where(Order.id == order_id).with_for_update()
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found", "ORDER_NOT_FOUND")

    if order.user_id != current_user.id:
        raise ForbiddenError(
            "Only the order owner can confirm payment", "FORBIDDEN"
        )

    current_time = now()

    if order.status == OrderStatus.PAID:
        qr_token = sign_qr_payload(
            order.id, order.qr_nonce, int(order.qr_expires_at.timestamp())
        )
        remaining = max(0, int((order.qr_expires_at - current_time).total_seconds()))
        return QrRefreshResponse(
            qr_token=qr_token,
            qr_expires_at=format_utc_z(order.qr_expires_at),
            qr_expires_at_ist=format_ist_iso(order.qr_expires_at),
            qr_seconds_remaining=remaining,
        )

    if order.status != OrderStatus.CREATED:
        raise ConflictError(
            f"Cannot confirm payment for order in status {order.status.value}",
            "INVALID_ORDER_STATUS",
        )

    if order.razorpay_order_id and payload.razorpay_order_id != order.razorpay_order_id:
        raise BadRequestError(
            "Razorpay order ID does not match", "INVALID_PAYMENT_SIGNATURE"
        )

    if not verify_payment_signature(
        payload.razorpay_order_id,
        payload.razorpay_payment_id,
        payload.razorpay_signature,
    ):
        raise BadRequestError(
            "Invalid Razorpay payment signature", "INVALID_PAYMENT_SIGNATURE"
        )

    order.status = OrderStatus.PAID
    order.paid_at = current_time
    order.razorpay_order_id = payload.razorpay_order_id
    order.razorpay_payment_id = payload.razorpay_payment_id
    order.updated_at = current_time

    await record_decision(
        tier=AuditTier.TIER_1,
        event_type=AuditEvent.PAYMENT_EVENT,
        input_state={
            "order_id": str(order.id),
            "razorpay_order_id": payload.razorpay_order_id,
            "razorpay_payment_id": payload.razorpay_payment_id,
        },
        decision="PAID",
        reason_code="PAYMENT_CONFIRMED",
        human_message=f"Payment confirmed for order {order.order_number}",
        latency_ms=0.0,
        order_id=order.id,
        actor_id=current_user.id,
        db=db,
    )
    await db.commit()

    qr_token = sign_qr_payload(
        order.id, order.qr_nonce, int(order.qr_expires_at.timestamp())
    )
    remaining = max(0, int((order.qr_expires_at - current_time).total_seconds()))
    return QrRefreshResponse(
        qr_token=qr_token,
        qr_expires_at=format_utc_z(order.qr_expires_at),
        qr_expires_at_ist=format_ist_iso(order.qr_expires_at),
        qr_seconds_remaining=remaining,
    )
