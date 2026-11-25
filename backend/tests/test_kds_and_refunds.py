import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock as clock_module
from app.core.config import settings
from app.core.security import hash_secret
from app.models.audit import AuditLog
from app.models.base import (
    AuditEvent,
    AuditTier,
    OrderStatus,
    PrepStage,
    RefundDecision,
    RefundStatus,
    UserRole,
)
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.refund import Refund
from app.models.user import User
from app.services.refund_engine import evaluate


def _non_surge_utc() -> datetime:
    return datetime(2026, 11, 25, 4, 30, 0, tzinfo=timezone.utc)


def _surge_utc() -> datetime:
    return datetime(2026, 11, 25, 7, 0, 0, tzinfo=timezone.utc)


async def _create_staff_and_manager_tokens(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> tuple[str, str]:
    staff_id = f"KDSSTF-{uuid.uuid4().hex[:6]}"
    mgr_id = f"KDSMGR-{uuid.uuid4().hex[:6]}"
    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                staff_id=staff_id,
                name="KDS Staff",
                role=UserRole.STAFF,
                pin_hash=await hash_secret("111111"),
                is_active=True,
            ),
            User(
                id=uuid.uuid4(),
                staff_id=mgr_id,
                name="Canteen Manager",
                role=UserRole.MANAGER,
                pin_hash=await hash_secret("222222"),
                is_active=True,
            ),
        ]
    )
    await db_session.commit()

    staff_login = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "111111"},
    )
    mgr_login = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": mgr_id, "pin": "222222"},
    )
    return staff_login.json()["access_token"], mgr_login.json()["access_token"]


async def _create_student_and_item(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    stock_count: int = 20,
) -> tuple[str, uuid.UUID, MenuItem]:
    email = f"kdsstu-{uuid.uuid4().hex[:8]}@campus.edu"
    reg = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "KDS Student", "password": "password123"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    student_id = uuid.UUID(reg.json()["user"]["id"])

    item = MenuItem(
        id=uuid.uuid4(),
        name=f"Dish {uuid.uuid4().hex[:6]}",
        description="Test dish",
        price_paise=12000,
        stock_count=stock_count,
        estimated_prep_minutes=6,
        is_available=True,
    )
    db_session.add(item)
    await db_session.commit()
    return token, student_id, item


async def _place_order(
    async_client: httpx.AsyncClient,
    token: str,
    item_id: uuid.UUID,
    qty: int = 1,
) -> dict:
    resp = await async_client.post(
        "/api/v1/orders",
        json={"items": [{"menu_item_id": str(item_id), "quantity": qty}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    return resp.json()


async def _seed_prior_refunds(
    db_session: AsyncSession,
    student_id: uuid.UUID,
    count: int,
    created_at: datetime,
) -> None:
    for idx in range(count):
        ord_row = Order(
            id=uuid.uuid4(),
            order_number=f"GC-HIST-{uuid.uuid4().hex[:6]}-{idx}",
            user_id=student_id,
            total_paise=5000,
            status=OrderStatus.REFUNDED,
            prep_stage=PrepStage.RECEIVED,
            qr_nonce=str(uuid.uuid4()),
            qr_expires_at=created_at + timedelta(minutes=5),
            created_at=created_at - timedelta(days=1),
            updated_at=created_at - timedelta(days=1),
        )
        db_session.add(ord_row)
        await db_session.flush()
        db_session.add(
            Refund(
                id=uuid.uuid4(),
                order_id=ord_row.id,
                user_id=student_id,
                prep_stage_at_request=PrepStage.RECEIVED,
                refund_count_7d=idx,
                surge_active=False,
                risk_score=0.0,
                factor_breakdown={"s_prep_norm": 0.0, "v_user_norm": 0.0, "l_surge": 0.0},
                decision=RefundDecision.AUTO_APPROVE,
                status=RefundStatus.COMPLETED,
                reason_code="RISK_LOW_PRE_PREP",
                human_message="Seeded prior refund",
                created_at=created_at - timedelta(days=1),
            )
        )
    await db_session.commit()


async def test_kds_transitions_and_etag_polling(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token, _ = await _create_staff_and_manager_tokens(async_client, db_session)
    student_token, _, item = await _create_student_and_item(async_client, db_session)
    order = await _place_order(async_client, student_token, item.id)
    order_id = order["id"]

    premature_ready = await async_client.post(
        f"/api/v1/kds/orders/{order_id}/mark-ready",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert premature_ready.status_code == 409

    start_resp = await async_client.post(
        f"/api/v1/kds/orders/{order_id}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert start_resp.status_code == 200
    assert start_resp.json()["prep_stage"] == "IN_PREP"

    repeat_start = await async_client.post(
        f"/api/v1/kds/orders/{order_id}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert repeat_start.status_code == 409

    ready_resp = await async_client.post(
        f"/api/v1/kds/orders/{order_id}/mark-ready",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert ready_resp.status_code == 200
    assert ready_resp.json()["prep_stage"] == "READY"

    audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.order_id == uuid.UUID(order_id),
                AuditLog.event_type == AuditEvent.STAGE_CHANGE,
            )
        )
    ).scalars().all()
    assert len(audits) == 2

    kds_first = await async_client.get(
        "/api/v1/kds/orders",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert kds_first.status_code == 200
    etag = kds_first.headers["ETag"]
    assert any(o["order_id"] == order_id and o["hold"] is False for o in kds_first.json()["orders"])

    kds_cached = await async_client.get(
        "/api/v1/kds/orders",
        headers={"Authorization": f"Bearer {staff_token}", "If-None-Match": etag},
    )
    assert kds_cached.status_code == 304


async def test_tier1_hard_blocks_and_audit_commits(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token, _ = await _create_staff_and_manager_tokens(async_client, db_session)
    student_token, student_id, item = await _create_student_and_item(async_client, db_session)
    order = await _place_order(async_client, student_token, item.id)

    verify_resp = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert verify_resp.status_code == 200

    cancel_collected = await async_client.post(
        f"/api/v1/orders/{order['id']}/cancel",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert cancel_collected.status_code == 409
    assert cancel_collected.json()["decision"] == "BLOCK"
    assert cancel_collected.json()["human_message"] == "Food already picked up"

    audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.order_id == uuid.UUID(order["id"]),
                AuditLog.event_type == AuditEvent.REFUND_EVAL,
                AuditLog.tier == AuditTier.TIER_1,
            )
        )
    ).scalars().all()
    assert len(audits) == 1

    current_time = datetime.now(timezone.utc)
    for status_val, expected_msg in (
        (OrderStatus.REFUNDED, "Already refunded"),
        (OrderStatus.PENDING_REVIEW, "Cancellation is already under manager review"),
    ):
        extra_order = Order(
            id=uuid.uuid4(),
            order_number=f"GC-HB-{uuid.uuid4().hex[:6]}",
            user_id=student_id,
            total_paise=5000,
            status=status_val,
            prep_stage=PrepStage.RECEIVED,
            qr_nonce=str(uuid.uuid4()),
            qr_expires_at=current_time + timedelta(minutes=5),
        )
        db_session.add(extra_order)
        await db_session.commit()

        resp = await async_client.post(
            f"/api/v1/orders/{extra_order.id}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
        assert resp.status_code == 409
        assert resp.json()["human_message"] == expected_msg


async def test_auto_approve_received_non_surge_restores_stock_and_revokes_qr(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token, _ = await _create_staff_and_manager_tokens(async_client, db_session)
    student_token, _, item = await _create_student_and_item(async_client, db_session, stock_count=5)
    order = await _place_order(async_client, student_token, item.id, qty=2)

    clock_module.set_clock_provider(_non_surge_utc)
    try:
        cancel_resp = await async_client.post(
            f"/api/v1/orders/{order['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
        kds_resp = await async_client.get(
            "/api/v1/kds/orders",
            headers={"Authorization": f"Bearer {staff_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)

    assert cancel_resp.status_code == 200
    body = cancel_resp.json()
    assert body["decision"] == "AUTO_APPROVE"
    assert body["status"] == "COMPLETED"
    assert body["risk_score"] < 0.3
    assert body["latency_ms"] < 200.0

    item_id = item.id
    db_session.expire_all()
    db_item = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert db_item.stock_count == 5

    verify_after = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert verify_after.status_code == 409

    assert any(
        c["order_number"] == order["order_number"]
        for c in kds_resp.json()["recently_cancelled"]
    )


async def test_stage_guard_in_prep_clean_student_escalates(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token, _ = await _create_staff_and_manager_tokens(async_client, db_session)
    student_token, _, item = await _create_student_and_item(async_client, db_session)
    order = await _place_order(async_client, student_token, item.id)

    await async_client.post(
        f"/api/v1/kds/orders/{order['id']}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    clock_module.set_clock_provider(_non_surge_utc)
    try:
        cancel_resp = await async_client.post(
            f"/api/v1/orders/{order['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)

    assert cancel_resp.status_code == 200
    body = cancel_resp.json()
    assert body["risk_score"] == 0.25
    assert body["decision"] == "ESCALATE"
    assert body["reason_code"] == "STAGE_GUARD_ESCALATION"
    assert "0.25" in body["human_message"]
    assert "preparation" in body["human_message"].lower()


async def test_escalate_and_block_thresholds_with_surge_and_formula_bounds(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token, mgr_token = await _create_staff_and_manager_tokens(async_client, db_session)
    student_token, student_id, item = await _create_student_and_item(async_client, db_session)
    await _seed_prior_refunds(db_session, student_id, 2, _non_surge_utc())

    order_esc = await _place_order(async_client, student_token, item.id)
    await async_client.post(
        f"/api/v1/kds/orders/{order_esc['id']}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    clock_module.set_clock_provider(_non_surge_utc)
    try:
        esc_resp = await async_client.post(
            f"/api/v1/orders/{order_esc['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
        queue_resp = await async_client.get(
            "/api/v1/manager/escalations",
            headers={"Authorization": f"Bearer {mgr_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)

    assert esc_resp.status_code == 200
    assert esc_resp.json()["risk_score"] == 0.4
    assert esc_resp.json()["decision"] == "ESCALATE"

    assert queue_resp.status_code == 200
    assert any(
        e["order_id"] == order_esc["id"] and e["is_overdue"] is False
        for e in queue_resp.json()
    )

    await _seed_prior_refunds(db_session, student_id, 1, _surge_utc())

    order_blk = await _place_order(async_client, student_token, item.id)
    await async_client.post(
        f"/api/v1/kds/orders/{order_blk['id']}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    clock_module.set_clock_provider(_surge_utc)
    try:
        blk_resp = await async_client.post(
            f"/api/v1/orders/{order_blk['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)

    assert blk_resp.status_code == 200
    blk_body = blk_resp.json()
    assert blk_body["risk_score"] == 0.75
    assert blk_body["decision"] == "BLOCK"
    assert blk_body["status"] == "BLOCKED"
    assert "IN_PREP" in blk_body["human_message"]
    assert "4" in blk_body["human_message"]

    verify_blocked = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": order_blk["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert verify_blocked.status_code == 200

    order_non_surge = await _place_order(async_client, student_token, item.id)
    await async_client.post(
        f"/api/v1/kds/orders/{order_non_surge['id']}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    clock_module.set_clock_provider(_non_surge_utc)
    try:
        non_surge_resp = await async_client.post(
            f"/api/v1/orders/{order_non_surge['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)
    assert non_surge_resp.json()["risk_score"] == 0.55
    assert non_surge_resp.json()["decision"] == "ESCALATE"

    dummy_ready = Order(prep_stage=PrepStage.READY)
    max_eval = evaluate(dummy_ready, v_user_count=6, eval_now=_surge_utc())
    assert max_eval.risk_score == 1.0


async def test_repeat_cancel_idempotency_and_unpaid_cancel(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    student_token, student_id, item = await _create_student_and_item(
        async_client, db_session, stock_count=10
    )

    order_auto = await _place_order(async_client, student_token, item.id)
    clock_module.set_clock_provider(_non_surge_utc)
    try:
        first = await async_client.post(
            f"/api/v1/orders/{order_auto['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
        second = await async_client.post(
            f"/api/v1/orders/{order_auto['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["reason_code"] == "ALREADY_EVALUATED"
    assert second.json()["decision"] == "AUTO_APPROVE"

    unpaid_order = Order(
        id=uuid.uuid4(),
        order_number=f"GC-UNP-{uuid.uuid4().hex[:6]}",
        user_id=student_id,
        total_paise=item.price_paise,
        status=OrderStatus.CREATED,
        prep_stage=PrepStage.RECEIVED,
        qr_nonce=str(uuid.uuid4()),
        qr_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(unpaid_order)
    await db_session.commit()

    unpaid_resp = await async_client.post(
        f"/api/v1/orders/{unpaid_order.id}/cancel",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert unpaid_resp.status_code == 200
    assert unpaid_resp.json()["decision"] == "CANCELLED_UNPAID"

    refunds_for_unpaid = (
        await db_session.execute(select(Refund).where(Refund.order_id == unpaid_order.id))
    ).scalars().all()
    assert len(refunds_for_unpaid) == 0


async def test_manager_approve_reject_stock_rules_sla_and_rbac(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_token, mgr_token = await _create_staff_and_manager_tokens(async_client, db_session)
    student_token, student_id, item = await _create_student_and_item(
        async_client, db_session, stock_count=10
    )
    other_token, _, _ = await _create_student_and_item(async_client, db_session)

    await _seed_prior_refunds(db_session, student_id, 4, _non_surge_utc())

    order_recv = await _place_order(async_client, student_token, item.id, qty=2)
    clock_module.set_clock_provider(_non_surge_utc)
    try:
        esc_recv = await async_client.post(
            f"/api/v1/orders/{order_recv['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)
    assert esc_recv.json()["decision"] == "ESCALATE"

    non_owner_cancel = await async_client.post(
        f"/api/v1/orders/{order_recv['id']}/cancel",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert non_owner_cancel.status_code == 403

    staff_queue = await async_client.get(
        "/api/v1/manager/escalations",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert staff_queue.status_code == 403

    refund_recv = (
        await db_session.execute(
            select(Refund).where(Refund.order_id == uuid.UUID(order_recv["id"]))
        )
    ).scalar_one()

    overdue_time = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ESCALATION_SLA_MINUTES + 5
    )
    clock_module.set_clock_provider(lambda: overdue_time)
    try:
        overdue_queue = await async_client.get(
            "/api/v1/manager/escalations",
            headers={"Authorization": f"Bearer {mgr_token}"},
        )
        assert overdue_queue.status_code == 200
        matched = [e for e in overdue_queue.json() if e["refund_id"] == str(refund_recv.id)]
        assert len(matched) == 1
        assert matched[0]["is_overdue"] is True
    finally:
        clock_module.set_clock_provider(None)

    approve_recv = await async_client.post(
        f"/api/v1/manager/escalations/{refund_recv.id}/decision",
        json={"action": "APPROVE", "notes": "Verified pre-prep"},
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert approve_recv.status_code == 200
    assert approve_recv.json()["decision"] == "MANAGER_APPROVE"
    assert approve_recv.json()["order_status"] == "REFUNDED"

    repeat_decision = await async_client.post(
        f"/api/v1/manager/escalations/{refund_recv.id}/decision",
        json={"action": "REJECT"},
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert repeat_decision.status_code == 409

    item_id = item.id
    db_session.expire_all()
    db_item = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert db_item.stock_count == 10

    order_in_prep = await _place_order(async_client, student_token, item.id, qty=2)
    await async_client.post(
        f"/api/v1/kds/orders/{order_in_prep['id']}/start-cooking",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    clock_module.set_clock_provider(_non_surge_utc)
    try:
        await async_client.post(
            f"/api/v1/orders/{order_in_prep['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)

    refund_in_prep = (
        await db_session.execute(
            select(Refund).where(Refund.order_id == uuid.UUID(order_in_prep["id"]))
        )
    ).scalar_one()
    approve_in_prep = await async_client.post(
        f"/api/v1/manager/escalations/{refund_in_prep.id}/decision",
        json={"action": "APPROVE"},
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert approve_in_prep.status_code == 200

    item_id = item.id
    db_session.expire_all()
    db_item_after = (
        await db_session.execute(select(MenuItem).where(MenuItem.id == item_id))
    ).scalar_one()
    assert db_item_after.stock_count == 8

    order_rej = await _place_order(async_client, student_token, item.id, qty=1)
    clock_module.set_clock_provider(_non_surge_utc)
    try:
        await async_client.post(
            f"/api/v1/orders/{order_rej['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        clock_module.set_clock_provider(None)

    refund_rej = (
        await db_session.execute(
            select(Refund).where(Refund.order_id == uuid.UUID(order_rej["id"]))
        )
    ).scalar_one()
    reject_resp = await async_client.post(
        f"/api/v1/manager/escalations/{refund_rej.id}/decision",
        json={"action": "REJECT", "notes": "Pick up at counter"},
        headers={"Authorization": f"Bearer {mgr_token}"},
    )
    assert reject_resp.status_code == 200
    assert reject_resp.json()["decision"] == "MANAGER_REJECT"
    assert reject_resp.json()["order_status"] == "PAID"

    refresh_after_reject = await async_client.post(
        f"/api/v1/orders/{order_rej['id']}/refresh-qr",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert refresh_after_reject.status_code == 200


async def test_concurrent_cancel_requests_single_refund_row(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    student_token, _, item = await _create_student_and_item(async_client, db_session)
    order = await _place_order(async_client, student_token, item.id)

    async def do_cancel() -> httpx.Response:
        return await async_client.post(
            f"/api/v1/orders/{order['id']}/cancel",
            headers={"Authorization": f"Bearer {student_token}"},
        )

    clock_module.set_clock_provider(_non_surge_utc)
    try:
        responses = await asyncio.gather(*(do_cancel() for _ in range(4)))
    finally:
        clock_module.set_clock_provider(None)

    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 3

    refunds = (
        await db_session.execute(
            select(Refund).where(Refund.order_id == uuid.UUID(order["id"]))
        )
    ).scalars().all()
    assert len(refunds) == 1


async def test_all_eight_refund_formula_vectors(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    vectors = [
        (PrepStage.RECEIVED, 0, False, 0.0, RefundDecision.AUTO_APPROVE, "RISK_LOW_PRE_PREP"),
        (PrepStage.RECEIVED, 0, True, 0.2, RefundDecision.AUTO_APPROVE, "RISK_LOW_PRE_PREP"),
        (PrepStage.IN_PREP, 0, False, 0.25, RefundDecision.ESCALATE, "STAGE_GUARD_ESCALATION"),
        (PrepStage.IN_PREP, 0, True, 0.45, RefundDecision.ESCALATE, "RISK_MEDIUM"),
        (PrepStage.IN_PREP, 4, False, 0.55, RefundDecision.ESCALATE, "RISK_MEDIUM"),
        (PrepStage.READY, 0, True, 0.7, RefundDecision.BLOCK, "RISK_HIGH"),
        (PrepStage.IN_PREP, 4, True, 0.75, RefundDecision.BLOCK, "RISK_HIGH"),
    ]
    for stage, refunds_7d, surge, expected_r, expected_dec, expected_reason in vectors:
        eval_time = _surge_utc() if surge else _non_surge_utc()
        dummy_order = Order(prep_stage=stage)
        res = evaluate(dummy_order, v_user_count=refunds_7d, eval_now=eval_time)
        assert res.risk_score == expected_r
        assert res.decision == expected_dec
        assert res.reason_code == expected_reason

    staff_token, _ = await _create_staff_and_manager_tokens(async_client, db_session)
    student_token, _, item = await _create_student_and_item(async_client, db_session)
    collected_order = await _place_order(async_client, student_token, item.id)

    scan_resp = await async_client.post(
        "/api/v1/verify-qr",
        json={"qr_token": collected_order["qr_token"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert scan_resp.status_code == 200

    v8_resp = await async_client.post(
        f"/api/v1/orders/{collected_order['id']}/cancel",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert v8_resp.status_code == 409
    assert v8_resp.json()["decision"] == "BLOCK"
    assert v8_resp.json()["reason_code"] == "ALREADY_COLLECTED"
