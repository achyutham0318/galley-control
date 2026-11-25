import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.restaurant import DEFAULT_RESTAURANT_ID


class MenuItem(Base):
    __tablename__ = "menu_items"
    __table_args__ = (
        UniqueConstraint(
            "restaurant_id", "name", name="uq_menu_items_restaurant_name"
        ),
        CheckConstraint("price_paise > 0", name="ck_menu_items_price_positive"),
        CheckConstraint("stock_count >= 0", name="ck_menu_items_stock_non_negative"),
        CheckConstraint(
            "discount_percent >= 0 AND discount_percent <= 90",
            name="ck_menu_items_discount_percent_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
        default=DEFAULT_RESTAURANT_ID,
        server_default=text(f"'{DEFAULT_RESTAURANT_ID}'"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    category: Mapped[str] = mapped_column(
        String(60), nullable=False, default="General", server_default=text("'General'")
    )
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_count: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_prep_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    is_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"), index=True
    )
    discount_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_surplus_deal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"), index=True
    )
    surplus_note: Mapped[str | None] = mapped_column(String(160), nullable=True)
    surplus_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
