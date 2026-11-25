import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrderStatus, PrepStage
from app.models.restaurant import DEFAULT_RESTAURANT_ID

order_number_seq = Sequence("order_number_seq", start=1001, increment=1)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_orders_user_idempotency_key",
        ),
        CheckConstraint(
            "coins_redeemed >= 0", name="ck_orders_coins_redeemed_non_negative"
        ),
        CheckConstraint(
            "coin_discount_paise >= 0", name="ck_orders_coin_discount_non_negative"
        ),
        CheckConstraint(
            "cash_payable_paise >= 0", name="ck_orders_cash_payable_non_negative"
        ),
        CheckConstraint(
            "refund_coins_credited >= 0",
            name="ck_orders_refund_coins_credited_non_negative",
        ),
        Index("ix_orders_status_prep_stage", "status", "prep_stage"),
        Index("ix_orders_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_number: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        server_default=text("'GC-' || nextval('order_number_seq')::text"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id"),
        nullable=False,
        default=DEFAULT_RESTAURANT_ID,
        server_default=text(f"'{DEFAULT_RESTAURANT_ID}'"),
        index=True,
    )
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    coins_redeemed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    coin_discount_paise: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cash_payable_paise: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    refund_coins_credited: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.CREATED,
        server_default=text("'CREATED'"),
    )
    prep_stage: Mapped[PrepStage] = mapped_column(
        Enum(PrepStage, name="prep_stage"),
        nullable=False,
        default=PrepStage.RECEIVED,
        server_default=text("'RECEIVED'"),
    )
    qr_nonce: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    qr_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    qr_redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_via_scan: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    razorpay_order_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    razorpay_payment_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    in_prep_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refunded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    menu_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("menu_items.id"),
        nullable=False,
    )
    item_name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    original_unit_price_paise: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    discount_percent_applied: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    order: Mapped[Order] = relationship(back_populates="items")
