import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import time
from typing import Any
import uuid

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import redis as redis_module
from app.core.clock import now
from app.core.config import settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.redis import record_redis_error
from app.models.base import AuditEvent, AuditTier, OrderStatus
from app.models.order import Order
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.common import format_ist_iso, format_utc_z
from app.schemas.qr import (
    QrRefreshResponse,
    VerifyOrderItemSummary,
    VerifyOrderSummary,
    VerifyQrResponse,
)
from app.services.audit_service import record_decision
from app.services.wallet_service import earn_pickup_points


def sign_qr_payload(order_id: uuid.UUID, nonce: str, exp_unix: int) -> str:
    canonical = f"{order_id}|{nonce}|{exp_unix}"
    signature = hmac.new(
        settings.QR_HMAC_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    raw = f"{canonical}|{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_and_verify_qr_token(token: str) -> tuple[uuid.UUID, str, int]:
    padding = "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{token}{padding}".encode("ascii")).decode(
            "utf-8"
        )
        order_id_str, nonce, exp_str, signature = decoded.split("|", 3)
        order_id = uuid.UUID(order_id_str)
        exp_unix = int(exp_str)
        if not nonce:
            raise ValueError("Empty nonce")
    except Exception as exc:
        raise ValueError("Malformed QR token") from exc

    canonical = f"{order_id_str}|{nonce}|{exp_unix}"
    expected_signature = hmac.new(
        settings.QR_HMAC_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Invalid QR signature")

    return order_id, nonce, exp_unix


def verify_qr_token_signature(token: str) -> tuple[str, str, int]:
    order_id, nonce, exp_unix = parse_and_verify_qr_token(token)
    return str(order_id), nonce, exp_unix


def _build_order_summary(order: Order) -> VerifyOrderSummary:
    return VerifyOrderSummary(
        order_number=order.order_number,
        items=[
            VerifyOrderItemSummary(
                item_name_snapshot=item.item_name_snapshot,
                quantity=item.quantity,
                unit_price_paise=item.unit_price_paise,
            )
            for item in order.items
        ],
        total_paise=order.total_paise,
    )


async def refresh_order_qr(
    db: AsyncSession,
    order_id: uuid.UUID,
    current_user: User,
) -> QrRefreshResponse:
    start = time.perf_counter()
    stmt = select(Order).where(Order.id == order_id).with_for_update()
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found", "ORDER_NOT_FOUND")

    if order.user_id != current_user.id:
        raise ForbiddenError(
            "Only the order owner can refresh the QR code", "FORBIDDEN"
        )

    if order.status != OrderStatus.PAID:
        raise ConflictError(
            f"Cannot refresh QR for order in status {order.status.value}",
            "INVALID_ORDER_STATUS",
        )

    current_time = now()
    old_nonce = order.qr_nonce
    remaining_old = max(0, int((order.qr_expires_at - current_time).total_seconds()))
    revoke_ttl = remaining_old + settings.QR_REDIS_TTL_BUFFER_SECONDS

    try:
        client = redis_module.get_redis()
        await client.set(f"qr:revoked:{old_nonce}", "1", ex=revoke_ttl)
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()

    new_nonce = str(uuid.uuid4())
    new_expires_at = current_time + timedelta(seconds=settings.QR_EXPIRY_SECONDS)
    order.qr_nonce = new_nonce
    order.qr_expires_at = new_expires_at
    order.updated_at = current_time

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.TIER_1,
        event_type=AuditEvent.QR_REFRESH,
        input_state={
            "order_id": str(order.id),
            "old_nonce_prefix": old_nonce[:8],
            "new_nonce_prefix": new_nonce[:8],
        },
        decision="REFRESHED",
        reason_code="QR_REFRESHED",
        human_message=f"Student refreshed QR for order {order.order_number}",
        latency_ms=latency_ms,
        order_id=order.id,
        actor_id=current_user.id,
        db=db,
    )
    await db.commit()

    qr_token = sign_qr_payload(order.id, new_nonce, int(new_expires_at.timestamp()))
    return QrRefreshResponse(
        qr_token=qr_token,
        qr_expires_at=format_utc_z(new_expires_at),
        qr_expires_at_ist=format_ist_iso(new_expires_at),
        qr_seconds_remaining=settings.QR_EXPIRY_SECONDS,
    )


async def _delete_redeemed_key_if_created(created_key: str | None) -> None:
    if created_key is None:
        return
    try:
        client = redis_module.get_redis()
        await client.delete(created_key)
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()


async def _fetch_order_for_update(
    db: AsyncSession, order_id: uuid.UUID
) -> Order | None:
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
        .with_for_update()
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def verify_token(
    db: AsyncSession,
    token: str,
    actor_id: uuid.UUID,
    as_of: datetime | None = None,
    scanner_restaurant_id: uuid.UUID | None = None,
) -> tuple[int, VerifyQrResponse]:
    start = time.perf_counter()
    check_time = as_of if as_of is not None else now()
    if check_time.tzinfo is None:
        check_time = check_time.replace(tzinfo=timezone.utc)
    else:
        check_time = check_time.astimezone(timezone.utc)

    fallback_mode = False
    created_redeemed_key: str | None = None

    async def red_response(
        http_status: int,
        reason_code: str,
        message: str,
        order_id: uuid.UUID | None,
        input_state: dict[str, Any],
        redeemed_at: datetime | None = None,
        restaurant_id: uuid.UUID | None = None,
    ) -> tuple[int, VerifyQrResponse]:
        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        await record_decision(
            tier=AuditTier.TIER_1,
            event_type=AuditEvent.QR_VERIFY,
            input_state=input_state,
            decision="RED",
            reason_code=reason_code,
            human_message=message,
            latency_ms=latency_ms,
            order_id=order_id,
            actor_id=actor_id,
            restaurant_id=restaurant_id,
            db=None,
        )
        return http_status, VerifyQrResponse(
            flash="RED",
            beep="ERROR",
            reason_code=reason_code,
            message=message,
            latency_ms=latency_ms,
            redeemed_at=format_utc_z(redeemed_at) if redeemed_at else None,
            redeemed_at_ist=format_ist_iso(redeemed_at) if redeemed_at else None,
            order_summary=None,
            fallback_mode=fallback_mode,
        )

    try:
        order_id, nonce, exp_unix = parse_and_verify_qr_token(token)
    except ValueError:
        return await red_response(
            http_status=400,
            reason_code="INVALID_SIGNATURE",
            message="Tampered or invalid QR token",
            order_id=None,
            input_state={"token_length": len(token)},
        )

    if int(check_time.timestamp()) > exp_unix:
        return await red_response(
            http_status=410,
            reason_code="QR_EXPIRED",
            message="QR code expired. Ask student to tap Refresh QR.",
            order_id=order_id,
            input_state={"nonce_prefix": nonce[:8], "exp_unix": exp_unix},
        )

    redeemed_key = f"qr:redeemed:{nonce}"
    revoked_key = f"qr:revoked:{nonce}"

    try:
        client = redis_module.get_redis()
        if await client.exists(revoked_key):
            return await red_response(
                http_status=409,
                reason_code="NONCE_REVOKED",
                message="QR code has been revoked. Ask student to show the refreshed QR.",
                order_id=order_id,
                input_state={"nonce_prefix": nonce[:8]},
            )

        remaining_seconds = max(1, exp_unix - int(check_time.timestamp()))
        ttl = remaining_seconds + settings.QR_REDIS_TTL_BUFFER_SECONDS
        iso_as_of = format_utc_z(check_time)
        acquired = await client.set(redeemed_key, iso_as_of, nx=True, ex=ttl)
        if not acquired:
            stored_iso = await client.get(redeemed_key)
            if stored_iso:
                stored_dt = datetime.fromisoformat(stored_iso.replace("Z", "+00:00"))
            else:
                stored_dt = check_time
            formatted_ist = format_ist_iso(stored_dt)
            return await red_response(
                http_status=409,
                reason_code="REPLAY_DETECTED",
                message=f"BLOCKED - Already redeemed at {formatted_ist}",
                order_id=order_id,
                input_state={"nonce_prefix": nonce[:8], "replay": True},
                redeemed_at=stored_dt,
            )
        created_redeemed_key = redeemed_key
    except (RedisError, OSError, TimeoutError):
        fallback_mode = True
        await record_redis_error()

    try:
        order = await _fetch_order_for_update(db, order_id)
        if order is None:
            await _delete_redeemed_key_if_created(created_redeemed_key)
            return await red_response(
                http_status=404,
                reason_code="ORDER_NOT_FOUND",
                message="Order not found",
                order_id=None,
                input_state={
                    "requested_order_id": str(order_id),
                    "nonce_prefix": nonce[:8],
                },
            )

        if order.qr_nonce != nonce:
            await _delete_redeemed_key_if_created(created_redeemed_key)
            return await red_response(
                http_status=409,
                reason_code="NONCE_SUPERSEDED",
                message="QR code has been superseded by a newer token",
                order_id=order.id,
                input_state={"nonce_prefix": nonce[:8]},
            )

        if scanner_restaurant_id is not None and order.restaurant_id != scanner_restaurant_id:
            await _delete_redeemed_key_if_created(created_redeemed_key)
            rest_name = (
                await db.execute(
                    select(Restaurant.name).where(Restaurant.id == order.restaurant_id)
                )
            ).scalar_one_or_none() or "the assigned restaurant"
            return await red_response(
                http_status=403,
                reason_code="WRONG_RESTAURANT",
                message=f"Wrong counter: pick up this order at {rest_name}",
                order_id=order.id,
                input_state={
                    "nonce_prefix": nonce[:8],
                    "order_restaurant_id": str(order.restaurant_id),
                    "scanner_restaurant_id": str(scanner_restaurant_id),
                },
                restaurant_id=scanner_restaurant_id,
            )

        if order.status == OrderStatus.COLLECTED:
            await _delete_redeemed_key_if_created(created_redeemed_key)
            collected_dt = order.collected_at or order.qr_redeemed_at or check_time
            collected_ist = format_ist_iso(collected_dt)
            return await red_response(
                http_status=409,
                reason_code="ALREADY_COLLECTED",
                message=f"BLOCKED - Already redeemed at {collected_ist}",
                order_id=order.id,
                input_state={"nonce_prefix": nonce[:8], "status": order.status.value},
                redeemed_at=collected_dt,
            )

        if order.status != OrderStatus.PAID:
            status_reason_map = {
                OrderStatus.REFUNDED: "ORDER_REFUNDED",
                OrderStatus.CANCELLED: "ORDER_CANCELLED",
                OrderStatus.PENDING_REVIEW: "ORDER_UNDER_REVIEW",
                OrderStatus.CREATED: "ORDER_UNPAID",
            }
            reason_code = status_reason_map.get(order.status, "INVALID_ORDER_STATUS")
            await _delete_redeemed_key_if_created(created_redeemed_key)
            return await red_response(
                http_status=409,
                reason_code=reason_code,
                message=f"Order cannot be collected in status {order.status.value}",
                order_id=order.id,
                input_state={"nonce_prefix": nonce[:8], "status": order.status.value},
            )

        order.status = OrderStatus.COLLECTED
        order.qr_redeemed_at = check_time
        order.collected_at = check_time
        order.verified_via_scan = True
        order.updated_at = check_time

        points_earned = await earn_pickup_points(
            db=db,
            user_id=order.user_id,
            order_id=order.id,
            order_number=order.order_number,
            cash_payable_paise=order.cash_payable_paise,
        )

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        await record_decision(
            tier=AuditTier.TIER_1,
            event_type=AuditEvent.QR_VERIFY,
            input_state={
                "nonce_prefix": nonce[:8],
                "fallback_mode": fallback_mode,
            },
            decision="GREEN",
            reason_code="PICKUP_VERIFIED",
            human_message=f"Order {order.order_number} verified and collected",
            latency_ms=latency_ms,
            order_id=order.id,
            actor_id=actor_id,
            restaurant_id=order.restaurant_id,
            db=db,
        )
        await db.commit()

        return 200, VerifyQrResponse(
            flash="GREEN",
            beep="SUCCESS",
            reason_code="PICKUP_VERIFIED",
            message=f"Order {order.order_number} verified",
            latency_ms=latency_ms,
            redeemed_at=format_utc_z(check_time),
            redeemed_at_ist=format_ist_iso(check_time),
            order_summary=_build_order_summary(order),
            fallback_mode=fallback_mode,
            points_earned=points_earned,
        )
    except Exception:
        await _delete_redeemed_key_if_created(created_redeemed_key)
        await db.rollback()
        raise


async def manual_collect(
    db: AsyncSession,
    order_number: str,
    reason: str,
    actor_id: uuid.UUID,
) -> VerifyQrResponse:
    start = time.perf_counter()
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_number == order_number.strip())
        .with_for_update()
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFoundError("Order not found", "ORDER_NOT_FOUND")

    if order.status != OrderStatus.PAID:
        raise ConflictError(
            f"Order cannot be manually collected in status {order.status.value}",
            "INVALID_ORDER_STATUS",
        )

    current_time = now()
    old_nonce = order.qr_nonce
    order.status = OrderStatus.COLLECTED
    order.collected_at = current_time
    order.qr_redeemed_at = current_time
    order.verified_via_scan = False
    order.updated_at = current_time

    points_earned = await earn_pickup_points(
        db=db,
        user_id=order.user_id,
        order_id=order.id,
        order_number=order.order_number,
        cash_payable_paise=order.cash_payable_paise,
    )

    remaining = max(0, int((order.qr_expires_at - current_time).total_seconds()))
    revoke_ttl = remaining + settings.QR_REDIS_TTL_BUFFER_SECONDS
    try:
        client = redis_module.get_redis()
        await client.set(f"qr:revoked:{old_nonce}", "1", ex=revoke_ttl)
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()

    latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
    await record_decision(
        tier=AuditTier.HUMAN_OVERRIDE,
        event_type=AuditEvent.MANUAL_COLLECT,
        input_state={
            "order_number": order.order_number,
            "reason": reason,
        },
        decision="MANUAL_COLLECT",
        reason_code="MANUAL_OVERRIDE",
        human_message=reason,
        latency_ms=latency_ms,
        order_id=order.id,
        actor_id=actor_id,
        db=db,
    )
    await db.commit()

    return VerifyQrResponse(
        flash="GREEN",
        beep="SUCCESS",
        reason_code="MANUAL_COLLECT",
        message=f"Order {order.order_number} manually collected",
        latency_ms=latency_ms,
        redeemed_at=format_utc_z(current_time),
        redeemed_at_ist=format_ist_iso(current_time),
        order_summary=_build_order_summary(order),
        fallback_mode=False,
        points_earned=points_earned,
    )
