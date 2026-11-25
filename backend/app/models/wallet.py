import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, WalletTransactionType


class StudentWallet(Base):
    __tablename__ = "student_wallets"
    __table_args__ = (
        CheckConstraint(
            "loyalty_points >= 0", name="ck_student_wallets_loyalty_points_non_negative"
        ),
        CheckConstraint(
            "gold_coins >= 0", name="ck_student_wallets_gold_coins_non_negative"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    loyalty_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    gold_coins: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    lifetime_points_earned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    lifetime_coins_earned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        CheckConstraint(
            "balance_points_after >= 0",
            name="ck_wallet_tx_balance_points_non_negative",
        ),
        CheckConstraint(
            "balance_coins_after >= 0",
            name="ck_wallet_tx_balance_coins_non_negative",
        ),
        Index("ix_wallet_tx_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=True,
        index=True,
    )
    tx_type: Mapped[WalletTransactionType] = mapped_column(
        Enum(WalletTransactionType, name="wallet_tx_type"),
        nullable=False,
    )
    points_delta: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    coins_delta: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    balance_points_after: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_coins_after: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
