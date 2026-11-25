import hashlib
import hmac
import uuid

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_session_maker
from app.models.base import RefundStatus
from app.models.order import Order
from app.models.refund import Refund

_RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


async def create_payment_order(amount_paise: int, receipt: str) -> str:
    if settings.RAZORPAY_MOCK_MODE:
        return f"order_mock_{receipt}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{_RAZORPAY_API_BASE}/orders",
            json={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
            },
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
        )
        response.raise_for_status()
        return str(response.json()["id"])


def verify_payment_signature(
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> bool:
    payload = f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8")
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, razorpay_signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def initiate_refund_async(refund_id: uuid.UUID) -> None:
    async with async_session_maker() as db:
        stmt = select(Refund).where(Refund.id == refund_id).with_for_update()
        refund = (await db.execute(stmt)).scalar_one_or_none()
        if refund is None:
            return

        if (
            refund.status != RefundStatus.INITIATED
            or refund.razorpay_refund_id is not None
        ):
            return

        if settings.RAZORPAY_MOCK_MODE:
            refund.razorpay_refund_id = (
                f"rfnd_mock_{str(refund.id).replace('-', '')[:14]}"
            )
            await db.commit()
            return

        order = (
            await db.execute(select(Order).where(Order.id == refund.order_id))
        ).scalar_one_or_none()
        if order is None or not order.razorpay_payment_id:
            return

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{_RAZORPAY_API_BASE}/payments/{order.razorpay_payment_id}/refund",
                json={"amount": order.total_paise},
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            )
            response.raise_for_status()
            refund.razorpay_refund_id = str(response.json()["id"])
            await db.commit()
