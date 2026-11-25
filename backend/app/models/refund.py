import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PrepStage, RefundDecision, RefundStatus


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=False,
        unique=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    restaurant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id"),
        nullable=True,
        index=True,
    )
    prep_stage_at_request: Mapped[PrepStage] = mapped_column(
        Enum(PrepStage, name="prep_stage"), nullable=False
    )
    refund_count_7d: Mapped[int] = mapped_column(Integer, nullable=False)
    surge_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False)
    factor_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decision: Mapped[RefundDecision] = mapped_column(
        Enum(RefundDecision, name="refund_decision"), nullable=False
    )
    status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus, name="refund_status"), nullable=False
    )
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    human_message: Mapped[str] = mapped_column(Text, nullable=False)
    razorpay_refund_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    manager_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
