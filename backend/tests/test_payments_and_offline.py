from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core import redis as redis_module
from app.core.config import settings
from app.core.security import hash_secret
from app.models.audit import AuditLog
from app.models.base import (
    AuditEvent,
    OrderStatus,
    PrepStage,
    RefundDecision,
    RefundStatus,
    UserRole,
)
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.refund import Refund
from app.models.user import User
from app.services.lifecycle_service import run_sweep_once
from app.services.qr_service import sign_qr_payload


def _sign_webhook(raw_body: bytes) -> str:
    return hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def _sign_checkout(order_id: str, payment_id: str) -> str:
    payload = f"{order_id}|{payment_id}".encode("utf-8")
    return hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


async def _create_staff_token(
    async_client: httpx.AsyncClient, db_session: AsyncSession
) -> str:
    staff_id = f"OFFSTF-{uuid.uuid4().hex[:6]}"
    db_session.add(
        User(
            id=uuid.uuid4(),
            staff_id=staff_id,
            name="Offline Staff",
            role=UserRole.STAFF,
            pin_hash=await hash_secret("123456"),
            is_active=True,
        )
    )
    await db_session.commit()
    resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "123456"},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _create_student_and_item(
    async_client: httpx.AsyncClient, db_session: AsyncSession, stock_count: int = 10
) -> tuple[str, uuid.UUID, MenuItem]:
    email = f"paystu-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Pay Student", "password": "password123"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    student_id = uuid.UUID(reg.json()["user"]["id"])

    item = MenuItem(
        id=uuid.uuid4(),
        name=f"PayMeal {uuid.uuid4().hex[:6]}",
        description="Test",
        price_paise=10000,
        stock_count=stock_count,
        estimated_prep_minutes=5,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()
    return token, student_id, item


async def test_webhook_signature_validation_and_dedup(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, student_id, _ = await _create_student_and_item(async_client, db_session)
    rzp_order_id = f"order_wh_{uuid.uuid4().hex[:8]}"
    order = Order(
        id=uuid.uuid4(),
        order_number=f"GC-WH-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=10000,
        status=OrderStatus.CREATED,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        razorpay_order_id=rzp_order_id,
    )
    db_session.add(order)
    await db_session.commit()

    event_id = f"evt_{uuid.uuid4().hex[:10]}"
    body_bytes = json.dumps(
        {
            "event": "payment.captured",
            "event_id": event_id,
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{uuid.uuid4().hex[:8]}",
                        "order_id": rzp_order_id,
                        "amount": 10000,
                    }
                }
            },
        }
    ).encode("utf-8")

    bad_resp = await async_client.post(
        "/api/v1/webhooks/razorpay",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": "deadbeef",
        },
    )
    assert bad_resp.status_code == 400
    assert bad_resp.json()["code"] == "INVALID_WEBHOOK_SIGNATURE"

    ok_resp = await async_client.post(
        "/api/v1/webhooks/razorpay",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(body_bytes),
        },
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["status"] == "processed"

    dup_resp = await async_client.post(
        "/api/v1/webhooks/razorpay",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(body_bytes),
        },
    )
    assert dup_resp.status_code == 200
    assert dup_resp.json()["status"] == "duplicate"

    order_id = order.id
    db_session.expire_all()
    refreshed = (
        await db_session.execute(select(Order).where(Order.id == order_id))
    ).scalar_one()
    assert refreshed.status == OrderStatus.PAID


async def test_webhook_refund_processed_idempotent_and_amount_mismatch(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, student_id, _ = await _create_student_and_item(async_client, db_session)
    rzp_order_id = f"order_mm_{uuid.uuid4().hex[:8]}"
    mismatch_order = Order(
        id=uuid.uuid4(),
        order_number=f"GC-MM-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=10000,
        status=OrderStatus.CREATED,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        razorpay_order_id=rzp_order_id,
    )
    db_session.add(mismatch_order)
    await db_session.commit()

    mismatch_body = json.dumps(
        {
            "event": "payment.captured",
            "event_id": f"evt_mm_{uuid.uuid4().hex[:8]}",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_mm_1",
                        "order_id": rzp_order_id,
                        "amount": 5000,
                    }
                }
            },
        }
    ).encode("utf-8")
    mm_resp = await async_client.post(
        "/api/v1/webhooks/razorpay",
        content=mismatch_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(mismatch_body),
        },
    )
    assert mm_resp.status_code == 200

    mismatch_order_id = mismatch_order.id
    db_session.expire_all()
    refreshed_mm = (
        await db_session.execute(select(Order).where(Order.id == mismatch_order_id))
    ).scalar_one()
    assert refreshed_mm.status == OrderStatus.CREATED

    rzp_refund_id = f"rfnd_{uuid.uuid4().hex[:10]}"
    paid_order = Order(
        id=uuid.uuid4(),
        order_number=f"GC-RFND-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=10000,
        status=OrderStatus.REFUNDED,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(paid_order)
    await db_session.flush()
    refund_row = Refund(
        id=uuid.uuid4(),
        order_id=paid_order.id,
        user_id=student_id,
        prep_stage_at_request=PrepStage.RECEIVED,
        refund_count_7d=0,
        surge_active=False,
        risk_score=0.0,
        factor_breakdown={"s_prep_norm": 0.0, "v_user_norm": 0.0, "l_surge": 0.0},
        decision=RefundDecision.AUTO_APPROVE,
        status=RefundStatus.INITIATED,
        reason_code="RISK_LOW_PRE_PREP",
        human_message="Approved",
        razorpay_refund_id=rzp_refund_id,
    )
    db_session.add(refund_row)
    await db_session.commit()

    for idx in range(2):
        rfnd_body = json.dumps(
            {
                "event": "refund.processed",
                "event_id": f"evt_rf_{idx}_{uuid.uuid4().hex[:6]}",
                "payload": {"refund": {"entity": {"id": rzp_refund_id}}},
            }
        ).encode("utf-8")
        r = await async_client.post(
            "/api/v1/webhooks/razorpay",
            content=rfnd_body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": _sign_webhook(rfnd_body),
            },
        )
        assert r.status_code == 200

    refund_row_id = refund_row.id
    db_session.expire_all()
    refreshed_rfnd = (
        await db_session.execute(select(Refund).where(Refund.id == refund_row_id))
    ).scalar_one()
    assert refreshed_rfnd.status == RefundStatus.COMPLETED
    assert refreshed_rfnd.resolved_at is not None


async def test_confirm_payment_and_nonce_snapshot(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token = await _create_staff_token(async_client, db_session)
    student_token, student_id, _ = await _create_student_and_item(
        async_client, db_session
    )
    rzp_order_id = f"order_cp_{uuid.uuid4().hex[:8]}"
    rzp_payment_id = f"pay_cp_{uuid.uuid4().hex[:8]}"
    nonce = str(uuid.uuid4())
    order = Order(
        id=uuid.uuid4(),
        order_number=f"GC-CP-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=10000,
        status=OrderStatus.CREATED,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=nonce,
        qr_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        razorpay_order_id=rzp_order_id,
    )
    db_session.add(order)
    await db_session.commit()

    bad_sig = await async_client.post(
        f"/api/v1/orders/{order.id}/confirm-payment",
        json={
            "razorpay_order_id": rzp_order_id,
            "razorpay_payment_id": rzp_payment_id,
            "razorpay_signature": "invalid",
        },
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert bad_sig.status_code == 400

    valid_sig = _sign_checkout(rzp_order_id, rzp_payment_id)
    ok_resp = await async_client.post(
        f"/api/v1/orders/{order.id}/confirm-payment",
        json={
            "razorpay_order_id": rzp_order_id,
            "razorpay_payment_id": rzp_payment_id,
            "razorpay_signature": valid_sig,
        },
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert ok_resp.status_code == 200
    assert "qr_token" in ok_resp.json()

    idem_resp = await async_client.post(
        f"/api/v1/orders/{order.id}/confirm-payment",
        json={
            "razorpay_order_id": rzp_order_id,
            "razorpay_payment_id": rzp_payment_id,
            "razorpay_signature": valid_sig,
        },
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert idem_resp.status_code == 200
    assert idem_resp.json()["qr_token"] == ok_resp.json()["qr_token"]

    snap_resp = await async_client.get(
        "/api/v1/verify-qr/nonce-snapshot",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert snap_resp.status_code == 200
    snap = snap_resp.json()
    assert snap["ttl_seconds"] == 60
    expected_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    assert expected_hash in snap["active_nonce_hashes"]


async def test_offline_sync_expiry_basis_replay_conflict_and_idempotency(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token = await _create_staff_token(async_client, db_session)
    student_token, _, item = await _create_student_and_item(async_client, db_session)

    ord_resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert ord_resp.status_code == 201
    order_data = ord_resp.json()
    qr_token = order_data["qr_token"]

    issued_time = datetime.now(timezone.utc)
    scanned_before_expiry = issued_time + timedelta(seconds=60)
    sync_after_expiry = issued_time + timedelta(seconds=settings.QR_EXPIRY_SECONDS + 120)

    scan_id_1 = str(uuid.uuid4())
    scan_id_2 = str(uuid.uuid4())
    batch_payload = {
        "items": [
            {
                "client_scan_id": scan_id_2,
                "qr_payload": qr_token,
                "scanned_at": (scanned_before_expiry + timedelta(seconds=10)).isoformat(),
            },
            {
                "client_scan_id": scan_id_1,
                "qr_payload": qr_token,
                "scanned_at": scanned_before_expiry.isoformat(),
            },
        ]
    }

    clock_module.set_clock_provider(lambda: sync_after_expiry)
    try:
        sync_resp = await async_client.post(
            "/api/v1/verify-qr/offline-sync",
            json=batch_payload,
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert sync_resp.status_code == 200
        results = sync_resp.json()["results"]
        assert results[0]["client_scan_id"] == scan_id_1
        assert results[0]["status"] == "VERIFIED"
        assert results[1]["client_scan_id"] == scan_id_2
        assert results[1]["status"] == "CONFLICT_REPLAY"

        retry_resp = await async_client.post(
            "/api/v1/verify-qr/offline-sync",
            json=batch_payload,
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert retry_resp.status_code == 200
        assert retry_resp.json()["results"] == results
    finally:
        clock_module.set_clock_provider(None)

    conflicts = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == AuditEvent.OFFLINE_SYNC_CONFLICT,
                AuditLog.input_state["client_scan_id"].astext == scan_id_2,
            )
        )
    ).scalars().all()
    assert len(conflicts) == 1


async def test_lifecycle_sweep_redrives_refund_and_cancels_stale_order(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, student_id, item = await _create_student_and_item(
        async_client, db_session, stock_count=8
    )
    old_time = datetime.now(timezone.utc) - timedelta(
        minutes=settings.ORDER_PAYMENT_TIMEOUT_MINUTES + 5
    )

    stale_order = Order(
        id=uuid.uuid4(),
        order_number=f"GC-STALE-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=item.price_paise * 3,
        status=OrderStatus.CREATED,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=old_time + timedelta(minutes=5),
        created_at=old_time,
        updated_at=old_time,
    )
    db_session.add(stale_order)
    await db_session.flush()
    db_session.add(
        OrderItem(
            id=uuid.uuid4(),
            order_id=stale_order.id,
            menu_item_id=item.id,
            item_name_snapshot=item.name,
            quantity=3,
            unit_price_paise=item.price_paise,
        )
    )

    paid_order = Order(
        id=uuid.uuid4(),
        order_number=f"GC-STUCK-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=item.price_paise,
        status=OrderStatus.REFUNDED,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=old_time + timedelta(minutes=5),
        created_at=old_time,
        updated_at=old_time,
    )
    db_session.add(paid_order)
    await db_session.flush()
    stuck_refund = Refund(
        id=uuid.uuid4(),
        order_id=paid_order.id,
        user_id=student_id,
        prep_stage_at_request=PrepStage.RECEIVED,
        refund_count_7d=0,
        surge_active=False,
        risk_score=0.0,
        factor_breakdown={"s_prep_norm": 0.0, "v_user_norm": 0.0, "l_surge": 0.0},
        decision=RefundDecision.AUTO_APPROVE,
        status=RefundStatus.INITIATED,
        reason_code="RISK_LOW_PRE_PREP",
        human_message="Stuck refund",
        razorpay_refund_id=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(stuck_refund)
    await db_session.commit()

    sweep_stats = await run_sweep_once()
    assert sweep_stats["cancelled_orders"] >= 1
    assert sweep_stats["retried_refunds"] >= 1

    stale_order_id = stale_order.id
    item_id = item.id
    stuck_refund_id = stuck_refund.id
    db_session.expire_all()
    refreshed_stale = (
        await db_session.execute(select(Order).where(Order.id == stale_order_id))
    ).scalar_one()
    assert refreshed_stale.status == OrderStatus.CANCELLED

    refreshed_item = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert refreshed_item.stock_count == 11

    refreshed_refund = (
        await db_session.execute(select(Refund).where(Refund.id == stuck_refund_id))
    ).scalar_one()
    assert refreshed_refund.razorpay_refund_id is not None
    assert refreshed_refund.razorpay_refund_id.startswith("rfnd_mock_")


async def test_offline_sync_exact_expiry_boundary_and_redis_recovery(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staff_token = await _create_staff_token(async_client, db_session)
    student_token, _, item = await _create_student_and_item(async_client, db_session)

    ord_1 = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {student_token}"},
        )
    ).json()

    exact_expiry = datetime.fromisoformat(ord_1["qr_expires_at"].replace("Z", "+00:00"))
    after_expiry = exact_expiry + timedelta(minutes=2)
    clock_module.set_clock_provider(lambda: after_expiry)
    try:
        boundary_sync = await async_client.post(
            "/api/v1/verify-qr/offline-sync",
            json={
                "items": [
                    {
                        "client_scan_id": str(uuid.uuid4()),
                        "qr_payload": ord_1["qr_token"],
                        "scanned_at": exact_expiry.isoformat(),
                    }
                ]
            },
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert boundary_sync.status_code == 200
        assert boundary_sync.json()["results"][0]["status"] == "VERIFIED"
    finally:
        clock_module.set_clock_provider(None)

    ord_2 = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {student_token}"},
        )
    ).json()
    ord_3 = (
        await async_client.post(
            "/api/v1/orders",
            json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
            headers={"Authorization": f"Bearer {student_token}"},
        )
    ).json()

    unreachable_redis = Redis.from_url(
        "redis://127.0.0.1:6399/0",
        decode_responses=True,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    monkeypatch.setattr(redis_module, "get_redis", lambda: unreachable_redis)

    try:
        degraded = await async_client.get("/api/v1/health")
        assert degraded.status_code == 200
        assert degraded.json()["status"] == "degraded"

        scan_down = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": ord_2["qr_token"]},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert scan_down.status_code == 200
        assert scan_down.json()["fallback_mode"] is True

        replay_down = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": ord_2["qr_token"]},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert replay_down.status_code == 409
    finally:
        await unreachable_redis.aclose()
        monkeypatch.undo()

    recovered_health = await async_client.get("/api/v1/health")
    assert recovered_health.status_code == 200
    assert recovered_health.json()["status"] == "healthy"

    scan_recovered = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": ord_3["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert scan_recovered.status_code == 200
    assert scan_recovered.json()["fallback_mode"] is False
