import asyncio
import base64
from datetime import datetime, timedelta, timezone
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
from app.models.base import AuditEvent, AuditTier, OrderStatus, PrepStage, UserRole
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.user import User
from app.services import qr_service
from app.services.qr_service import sign_qr_payload


async def _create_staff_and_token(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    prefix: str = "STF",
) -> tuple[User, str]:
    staff_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    user = User(
        id=uuid.uuid4(),
        staff_id=staff_id,
        name=f"Staff {staff_id}",
        role=UserRole.STAFF,
        pin_hash=await hash_secret("123456"),
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "123456"},
    )
    assert resp.status_code == 200
    return user, resp.json()["access_token"]


async def _create_student_and_paid_order(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    prefix: str = "stu",
) -> tuple[str, uuid.UUID, dict]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": f"Student {prefix}", "password": "password123"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    student_id = uuid.UUID(reg.json()["user"]["id"])

    item = MenuItem(
        id=uuid.uuid4(),
        name=f"Meal {uuid.uuid4().hex[:6]}",
        description="Test meal",
        price_paise=10000,
        stock_count=20,
        estimated_prep_minutes=5,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()

    order_resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item.id), "quantity": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert order_resp.status_code == 201
    return token, student_id, order_resp.json()


async def test_first_scan_green_and_second_scan_replay_ist(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    _, _, order = await _create_student_and_paid_order(async_client, db_session)

    first = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["flash"] == "GREEN"
    assert first_body["beep"] == "SUCCESS"
    assert first_body["reason_code"] == "PICKUP_VERIFIED"
    assert first_body["order_summary"]["order_number"] == order["order_number"]
    assert isinstance(first_body["latency_ms"], float)

    db_session.expire_all()
    db_order = (
        await db_session.execute(select(Order).where(Order.id == uuid.UUID(order["id"])))
    ).scalar_one()
    assert db_order.status == OrderStatus.COLLECTED
    assert db_order.verified_via_scan is True

    audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.order_id == uuid.UUID(order["id"]),
                AuditLog.decision == "GREEN",
            )
        )
    ).scalars().all()
    assert len(audits) == 1

    second = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert second.status_code == 409
    second_body = second.json()
    assert second_body["flash"] == "RED"
    assert second_body["beep"] == "ERROR"
    assert second_body["reason_code"] == "REPLAY_DETECTED"
    assert first_body["redeemed_at_ist"] in second_body["message"]


async def test_concurrent_ten_scans_single_winner(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    _, _, order = await _create_student_and_paid_order(async_client, db_session)

    async def scan() -> httpx.Response:
        return await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": order["qr_token"]},
            headers={"Authorization": f"Bearer {staff_token}"},
        )

    responses = await asyncio.gather(*(scan() for _ in range(10)))
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 9

    green_audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.order_id == uuid.UUID(order["id"]),
                AuditLog.decision == "GREEN",
            )
        )
    ).scalars().all()
    assert len(green_audits) == 1


async def test_tampered_qr_rejected_with_audit_row(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_user, staff_token = await _create_staff_and_token(async_client, db_session)
    _, _, order = await _create_student_and_paid_order(async_client, db_session)

    padding = "=" * (-len(order["qr_token"]) % 4)
    decoded = base64.urlsafe_b64decode(f"{order['qr_token']}{padding}".encode("ascii")).decode("utf-8")
    oid, nonce, exp_s, sig = decoded.split("|", 3)

    tampered_variants = [
        f"{oid}|{uuid.uuid4()}|{exp_s}|{sig}",
        f"{oid}|{nonce}|{int(exp_s) + 600}|{sig}",
        f"{uuid.uuid4()}|{nonce}|{exp_s}|{sig}",
    ]
    for raw in tampered_variants:
        tampered = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
        resp = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": tampered},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["flash"] == "RED"
        assert body["reason_code"] == "INVALID_SIGNATURE"

    audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.actor_id == staff_user.id,
                AuditLog.reason_code == "INVALID_SIGNATURE",
            )
        )
    ).scalars().all()
    assert len(audits) == 3
    for audit in audits:
        assert settings.QR_HMAC_SECRET not in str(audit.input_state)


async def test_expired_qr_then_refresh_blocks_old_and_allows_new(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    student_token, _, order = await _create_student_and_paid_order(async_client, db_session)
    old_qr = order["qr_token"]

    future = datetime.now(timezone.utc) + timedelta(seconds=settings.QR_EXPIRY_SECONDS + 30)
    clock_module.set_clock_provider(lambda: future)
    try:
        expired_resp = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": old_qr},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert expired_resp.status_code == 410
        assert expired_resp.json()["reason_code"] == "QR_EXPIRED"
    finally:
        clock_module.set_clock_provider(None)

    refresh_resp = await async_client.post(
        f"/api/v1/orders/{order['id']}/refresh-qr",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert refresh_resp.status_code == 200
    new_qr = refresh_resp.json()["qr_token"]

    old_after_refresh = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": old_qr},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert old_after_refresh.status_code == 409
    assert old_after_refresh.json()["reason_code"] in {"NONCE_REVOKED", "NONCE_SUPERSEDED"}

    new_scan = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": new_qr},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert new_scan.status_code == 200
    assert new_scan.json()["flash"] == "GREEN"


async def test_old_screenshot_cannot_race_refreshed_qr(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    student_token, _, order = await _create_student_and_paid_order(async_client, db_session)
    old_qr = order["qr_token"]

    refresh_resp = await async_client.post(
        f"/api/v1/orders/{order['id']}/refresh-qr",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert refresh_resp.status_code == 200
    new_qr = refresh_resp.json()["qr_token"]

    old_task = async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": old_qr},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    new_task = async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": new_qr},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    old_res, new_res = await asyncio.gather(old_task, new_task)
    assert old_res.status_code == 409
    assert new_res.status_code == 200
    assert new_res.json()["flash"] == "GREEN"


async def test_redis_down_fallback_mode_and_nonce_supercession(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    student_token, _, order = await _create_student_and_paid_order(async_client, db_session)
    old_qr = order["qr_token"]

    refresh_resp = await async_client.post(
        f"/api/v1/orders/{order['id']}/refresh-qr",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert refresh_resp.status_code == 200
    new_qr = refresh_resp.json()["qr_token"]

    unreachable_redis = Redis.from_url(
        "redis://127.0.0.1:6399/0",
        decode_responses=True,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    monkeypatch.setattr(redis_module, "get_redis", lambda: unreachable_redis)

    try:
        old_scan = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": old_qr},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert old_scan.status_code == 409
        assert old_scan.json()["reason_code"] == "NONCE_SUPERSEDED"

        first_new = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": new_qr},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert first_new.status_code == 200
        assert first_new.json()["fallback_mode"] is True

        second_new = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": new_qr},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert second_new.status_code == 409
        assert second_new.json()["reason_code"] == "ALREADY_COLLECTED"
        assert first_new.json()["redeemed_at_ist"] in second_new.json()["message"]
    finally:
        await unreachable_redis.aclose()


async def test_set_nx_rollback_on_step4_exception(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    _, _, order = await _create_student_and_paid_order(async_client, db_session)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Simulated DB failure in step 4")

    monkeypatch.setattr(qr_service, "_fetch_order_for_update", boom)
    with pytest.raises(RuntimeError):
        await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": order["qr_token"]},
            headers={"Authorization": f"Bearer {staff_token}"},
        )

    monkeypatch.undo()
    retry_resp = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert retry_resp.status_code == 200
    assert retry_resp.json()["flash"] == "GREEN"


async def test_invalid_order_statuses_rejected_without_poisoning_redis(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    _, student_id, _ = await _create_student_and_paid_order(async_client, db_session)

    status_cases = [
        (OrderStatus.CREATED, "ORDER_UNPAID"),
        (OrderStatus.PENDING_REVIEW, "ORDER_UNDER_REVIEW"),
        (OrderStatus.REFUNDED, "ORDER_REFUNDED"),
        (OrderStatus.CANCELLED, "ORDER_CANCELLED"),
    ]
    current_time = datetime.now(timezone.utc)
    for st, expected_reason in status_cases:
        nonce = str(uuid.uuid4())
        exp = current_time + timedelta(minutes=5)
        ord_row = Order(
            id=uuid.uuid4(),
            order_number=f"GC-ST-{uuid.uuid4().hex[:6]}",
            user_id=student_id,
            total_paise=5000,
            status=st,
            prep_stage=PrepStage.RECEIVED,
            qr_nonce=nonce,
            qr_expires_at=exp,
        )
        db_session.add(ord_row)
        await db_session.commit()

        tok = sign_qr_payload(ord_row.id, nonce, int(exp.timestamp()))
        resp = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": tok},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["reason_code"] == expected_reason

        redis_client = redis_module.get_redis()
        assert await redis_client.exists(f"qr:redeemed:{nonce}") == 0

        red_audits = (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.order_id == ord_row.id,
                    AuditLog.decision == "RED",
                    AuditLog.reason_code == expected_reason,
                )
            )
        ).scalars().all()
        assert len(red_audits) == 1


async def test_refresh_qr_status_and_ownership_rules(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner_token, owner_id, paid_order = await _create_student_and_paid_order(
        async_client, db_session, prefix="ownref"
    )
    other_token, _, _ = await _create_student_and_paid_order(
        async_client, db_session, prefix="othref"
    )

    forbidden_resp = await async_client.post(
        f"/api/v1/orders/{paid_order['id']}/refresh-qr",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert forbidden_resp.status_code == 403

    ok_resp = await async_client.post(
        f"/api/v1/orders/{paid_order['id']}/refresh-qr",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert ok_resp.status_code == 200

    current_time = datetime.now(timezone.utc)
    for blocked_status in (
        OrderStatus.COLLECTED,
        OrderStatus.REFUNDED,
        OrderStatus.PENDING_REVIEW,
        OrderStatus.CANCELLED,
        OrderStatus.CREATED,
    ):
        ord_row = Order(
            id=uuid.uuid4(),
            order_number=f"GC-RF-{uuid.uuid4().hex[:6]}",
            user_id=owner_id,
            total_paise=5000,
            status=blocked_status,
            prep_stage=PrepStage.RECEIVED,
            qr_nonce=str(uuid.uuid4()),
            qr_expires_at=current_time + timedelta(minutes=5),
        )
        db_session.add(ord_row)
        await db_session.commit()

        resp = await async_client.post(
            f"/api/v1/orders/{ord_row.id}/refresh-qr",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 409


async def test_manual_collect_override_and_subsequent_qr_blocked(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session)
    _, _, order = await _create_student_and_paid_order(async_client, db_session)

    blank_reason_resp = await async_client.post(
        "/api/v1/verify-qr/manual-collect",
        json={"order_number": order["order_number"], "reason": "   "},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert blank_reason_resp.status_code == 422

    manual_resp = await async_client.post(
        "/api/v1/verify-qr/manual-collect",
        json={
            "order_number": order["order_number"],
            "reason": "Student phone battery died at counter",
        },
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert manual_resp.status_code == 200
    assert manual_resp.json()["flash"] == "GREEN"

    db_session.expire_all()
    db_order = (
        await db_session.execute(select(Order).where(Order.id == uuid.UUID(order["id"])))
    ).scalar_one()
    assert db_order.status == OrderStatus.COLLECTED
    assert db_order.verified_via_scan is False

    audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.order_id == db_order.id,
                AuditLog.tier == AuditTier.HUMAN_OVERRIDE,
                AuditLog.event_type == AuditEvent.MANUAL_COLLECT,
            )
        )
    ).scalars().all()
    assert len(audits) == 1

    second_manual = await async_client.post(
        "/api/v1/verify-qr/manual-collect",
        json={
            "order_number": order["order_number"],
            "reason": "Duplicate manual attempt",
        },
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert second_manual.status_code == 409

    qr_after_manual = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert qr_after_manual.status_code == 409


async def test_verify_rbac_rate_limit_and_latency(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, staff_token = await _create_staff_and_token(async_client, db_session, prefix="RLV")
    student_token, _, order = await _create_student_and_paid_order(async_client, db_session)

    stu_verify = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert stu_verify.status_code == 403

    stu_manual = await async_client.post(
        "/api/v1/verify-qr/manual-collect",
        json={"order_number": order["order_number"], "reason": "Unauthorized"},
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert stu_manual.status_code == 403

    for _ in range(120):
        resp = await async_client.post(
            "/api/v1/verify-qr",
            json={"qr_token": "invalid-token-probe"},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["latency_ms"] < 150.0
        assert settings.QR_HMAC_SECRET not in resp.text

    rl_resp = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": "invalid-token-probe"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert rl_resp.status_code == 429
    assert "Retry-After" in rl_resp.headers
