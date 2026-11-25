from datetime import datetime, timedelta, timezone
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.api.v1.kds import mark_ready, start_cooking
from app.core import clock as clock_module
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import NotFoundError
from app.core.security import hash_secret
from app.models.audit import AuditLog
from app.models.base import (
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
from app.schemas.demo import (
    DemoScenarioName,
    DemoSimulateResponse,
    DemoStepResult,
    SimulateScenarioRequest,
)
from app.schemas.order import CreateOrderRequest, OrderItemRequest
from app.services.metrics_service import serialize_audit_entry
from app.services.order_service import create_order
from app.services.qr_service import verify_token
from app.services.refund_engine import cancel_order_with_refund_engine
from app.services.wallet_service import get_or_create_wallet

router = APIRouter(prefix="/demo", tags=["demo"])


def _surge_time_utc() -> datetime:
    today = datetime.now(timezone.utc).date()
    return datetime(today.year, today.month, today.day, 7, 0, 0, tzinfo=timezone.utc)


def _non_surge_time_utc() -> datetime:
    today = datetime.now(timezone.utc).date()
    return datetime(today.year, today.month, today.day, 5, 0, 0, tzinfo=timezone.utc)


async def _ensure_demo_item(db: AsyncSession) -> MenuItem:
    item = (
        await db.execute(
            select(MenuItem)
            .where(MenuItem.is_available.is_(True), MenuItem.stock_count >= 5)
            .order_by(MenuItem.name.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if item is not None:
        return item

    item = MenuItem(
        id=uuid.uuid4(),
        name=f"Demo Paneer Roll {uuid.uuid4().hex[:6]}",
        description="Demo canteen item",
        price_paise=12000,
        stock_count=50,
        estimated_prep_minutes=5,
        is_available=True,
    )
    db.add(item)
    await db.commit()
    return item


async def _ensure_clean_student(db: AsyncSession) -> User:
    student = User(
        id=uuid.uuid4(),
        email=f"demo-clean-{uuid.uuid4().hex[:8]}@campus.edu",
        name="Demo Clean Student",
        role=UserRole.STUDENT,
        password_hash=await hash_secret("Student@123"),
        is_active=True,
    )
    db.add(student)
    await db.commit()
    return student


async def _ensure_serial_abuser(db: AsyncSession) -> User:
    existing = (
        await db.execute(
            select(User).where(User.email == "abuser01@campus.edu")
        )
    ).scalar_one_or_none()
    if existing is not None:
        cutoff = clock_module.now() - timedelta(days=7)
        recent_count = int(
            (
                await db.execute(
                    select(func.count(Refund.id)).where(
                        Refund.user_id == existing.id,
                        Refund.created_at >= cutoff,
                    )
                )
            ).scalar_one()
            or 0
        )
        if recent_count >= 4:
            return existing
        abuser = existing
        needed = 4 - recent_count
    else:
        abuser = User(
            id=uuid.uuid4(),
            email=f"demo-abuser-{uuid.uuid4().hex[:8]}@campus.edu",
            name="Demo Serial Abuser",
            role=UserRole.STUDENT,
            password_hash=await hash_secret("Student@123"),
            is_active=True,
        )
        db.add(abuser)
        await db.flush()
        needed = 4

    current_time = clock_module.now()
    for idx in range(needed):
        past_order = Order(
            id=uuid.uuid4(),
            order_number=f"GC-DEMO-{uuid.uuid4().hex[:6]}-{idx}",
            user_id=abuser.id,
            total_paise=10000,
            status=OrderStatus.REFUNDED,
            prep_stage=PrepStage.RECEIVED,
            qr_nonce=str(uuid.uuid4()),
            qr_expires_at=current_time + timedelta(minutes=5),
            created_at=current_time - timedelta(days=1),
            updated_at=current_time - timedelta(days=1),
        )
        db.add(past_order)
        await db.flush()
        db.add(
            Refund(
                id=uuid.uuid4(),
                order_id=past_order.id,
                user_id=abuser.id,
                prep_stage_at_request=PrepStage.RECEIVED,
                refund_count_7d=idx,
                surge_active=False,
                risk_score=0.0,
                factor_breakdown={
                    "s_prep_norm": 0.0,
                    "v_user_norm": 0.0,
                    "l_surge": 0.0,
                },
                decision=RefundDecision.AUTO_APPROVE,
                status=RefundStatus.COMPLETED,
                reason_code="RISK_LOW_PRE_PREP",
                human_message="Prior demo refund",
                created_at=current_time - timedelta(days=1),
            )
        )
    await db.commit()
    return abuser


@router.post(
    "/simulate/{scenario}",
    response_model=DemoSimulateResponse,
    summary="Execute a live end-to-end operational or abuse scenario",
)
async def simulate_scenario(
    scenario: DemoScenarioName,
    background_tasks: BackgroundTasks,
    body: SimulateScenarioRequest | None = Body(default=None),
    surge_override: bool | None = Query(default=None),
    current_user: User = Depends(require_roles(UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> DemoSimulateResponse:
    if not settings.DEMO_MODE_ENABLED:
        raise NotFoundError("Demo mode is disabled", "DEMO_MODE_DISABLED")

    effective_surge = (
        surge_override
        if surge_override is not None
        else (body.surge_override if body is not None else None)
    )

    total_start = time.perf_counter()
    steps: list[DemoStepResult] = []
    final_result: dict = {}

    item = await _ensure_demo_item(db)
    actor = (
        await _ensure_serial_abuser(db)
        if scenario == "serial_abuse"
        else await _ensure_clean_student(db)
    )

    step_start = time.perf_counter()
    created_order = await create_order(
        db=db,
        user=actor,
        payload=CreateOrderRequest(
            items=[OrderItemRequest(menu_item_id=item.id, quantity=1)]
        ),
    )
    steps.append(
        DemoStepResult(
            step_name="create_order",
            status_code=201,
            latency_ms=round((time.perf_counter() - step_start) * 1000.0, 3),
            outcome={
                "order_id": str(created_order.id),
                "order_number": created_order.order_number,
                "status": created_order.status.value,
            },
        )
    )

    order_uuid = created_order.id
    qr_token = created_order.qr_token or ""

    if scenario == "normal_pickup":
        s1 = time.perf_counter()
        kds_prep = await start_cooking(order_id=order_uuid, current_user=current_user, db=db)
        steps.append(
            DemoStepResult(
                step_name="kds_start_cooking",
                status_code=200,
                latency_ms=round((time.perf_counter() - s1) * 1000.0, 3),
                outcome={"prep_stage": kds_prep.prep_stage.value},
            )
        )

        s2 = time.perf_counter()
        kds_ready = await mark_ready(order_id=order_uuid, current_user=current_user, db=db)
        steps.append(
            DemoStepResult(
                step_name="kds_mark_ready",
                status_code=200,
                latency_ms=round((time.perf_counter() - s2) * 1000.0, 3),
                outcome={"prep_stage": kds_ready.prep_stage.value},
            )
        )

        s3 = time.perf_counter()
        v_code, v_resp = await verify_token(
            db=db, token=qr_token, actor_id=current_user.id
        )
        steps.append(
            DemoStepResult(
                step_name="verify_qr",
                status_code=v_code,
                latency_ms=round((time.perf_counter() - s3) * 1000.0, 3),
                outcome=v_resp.model_dump(),
            )
        )
        final_result = v_resp.model_dump()
        wallet = await get_or_create_wallet(db, actor.id)
        final_result["loyalty_points_earned"] = v_resp.points_earned
        final_result["updated_wallet_points"] = wallet.loyalty_points

    elif scenario == "qr_replay":
        s1 = time.perf_counter()
        v1_code, v1_resp = await verify_token(
            db=db, token=qr_token, actor_id=current_user.id
        )
        steps.append(
            DemoStepResult(
                step_name="verify_qr_first_scan",
                status_code=v1_code,
                latency_ms=round((time.perf_counter() - s1) * 1000.0, 3),
                outcome=v1_resp.model_dump(),
            )
        )

        s2 = time.perf_counter()
        v2_code, v2_resp = await verify_token(
            db=db, token=qr_token, actor_id=current_user.id
        )
        steps.append(
            DemoStepResult(
                step_name="verify_qr_replay_scan",
                status_code=v2_code,
                latency_ms=round((time.perf_counter() - s2) * 1000.0, 3),
                outcome=v2_resp.model_dump(),
            )
        )
        final_result = v2_resp.model_dump()

    elif scenario == "pre_prep_cancel":
        if effective_surge is True:
            clock_module.set_clock_provider(_surge_time_utc)
        else:
            clock_module.set_clock_provider(_non_surge_time_utc)
        try:
            s1 = time.perf_counter()
            c_code, c_resp = await cancel_order_with_refund_engine(
                db=db,
                order_id=order_uuid,
                current_user=actor,
                background_tasks=background_tasks,
            )
            steps.append(
                DemoStepResult(
                    step_name="cancel_order_tier2",
                    status_code=c_code,
                    latency_ms=round((time.perf_counter() - s1) * 1000.0, 3),
                    outcome=c_resp.model_dump(),
                )
            )
            final_result = c_resp.model_dump()
            wallet = await get_or_create_wallet(db, actor.id)
            final_result["coins_credited"] = c_resp.coins_credited
            final_result["updated_wallet_gold_coins"] = wallet.gold_coins
        finally:
            clock_module.set_clock_provider(None)

    else:
        s1 = time.perf_counter()
        kds_prep = await start_cooking(order_id=order_uuid, current_user=current_user, db=db)
        steps.append(
            DemoStepResult(
                step_name="kds_start_cooking",
                status_code=200,
                latency_ms=round((time.perf_counter() - s1) * 1000.0, 3),
                outcome={"prep_stage": kds_prep.prep_stage.value},
            )
        )

        if effective_surge is False:
            clock_module.set_clock_provider(_non_surge_time_utc)
        else:
            clock_module.set_clock_provider(_surge_time_utc)
        try:
            s2 = time.perf_counter()
            c_code, c_resp = await cancel_order_with_refund_engine(
                db=db,
                order_id=order_uuid,
                current_user=actor,
                background_tasks=background_tasks,
            )
            steps.append(
                DemoStepResult(
                    step_name="cancel_order_tier2",
                    status_code=c_code,
                    latency_ms=round((time.perf_counter() - s2) * 1000.0, 3),
                    outcome=c_resp.model_dump(),
                )
            )
            final_result = c_resp.model_dump()
            wallet = await get_or_create_wallet(db, actor.id)
            final_result["coins_credited"] = c_resp.coins_credited
            final_result["updated_wallet_gold_coins"] = wallet.gold_coins
        finally:
            clock_module.set_clock_provider(None)

    audit_rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.order_id == order_uuid)
            .order_by(AuditLog.created_at.asc())
        )
    ).scalars().all()

    return DemoSimulateResponse(
        scenario=scenario,
        total_latency_ms=round((time.perf_counter() - total_start) * 1000.0, 3),
        steps=steps,
        final_result=final_result,
        audit_entries=[
            serialize_audit_entry(row, created_order.order_number)
            for row in audit_rows
        ],
    )
