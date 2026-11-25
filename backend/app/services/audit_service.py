from typing import Any
import uuid

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.database import open_independent_session
from app.models.audit import AuditLog
from app.models.base import AuditEvent, AuditTier


async def record_decision(
    tier: AuditTier,
    event_type: AuditEvent,
    input_state: dict[str, Any],
    decision: str,
    reason_code: str,
    human_message: str,
    latency_ms: float,
    order_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    risk_score: float | None = None,
    restaurant_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> AuditLog:
    entry = AuditLog(
        id=uuid.uuid4(),
        order_id=order_id,
        actor_id=actor_id,
        restaurant_id=restaurant_id,
        tier=tier,
        event_type=event_type,
        input_state=input_state,
        risk_score=risk_score,
        decision=decision,
        reason_code=reason_code,
        human_message=human_message,
        latency_ms=latency_ms,
        created_at=now(),
    )

    if db is not None:
        db.add(entry)
        return entry

    async with open_independent_session() as independent_db:
        independent_db.add(entry)
        await independent_db.commit()
        return entry


async def list_audit_logs_for_order(
    db: AsyncSession,
    order_id: uuid.UUID,
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.order_id == order_id)
        .order_by(desc(AuditLog.created_at))
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_recent_audit_logs(
    db: AsyncSession,
    limit: int = 50,
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
