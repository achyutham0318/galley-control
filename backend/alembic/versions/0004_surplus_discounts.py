from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_surplus_discounts"
down_revision: str | None = "0003_wallet_and_loyalty"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_event ADD VALUE IF NOT EXISTS 'SURPLUS_DISCOUNT_UPDATE'")

    op.add_column(
        "menu_items",
        sa.Column(
            "discount_percent",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "menu_items",
        sa.Column(
            "is_surplus_deal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "menu_items",
        sa.Column("surplus_note", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "menu_items",
        sa.Column("surplus_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_menu_items_discount_percent_range",
        "menu_items",
        "discount_percent >= 0 AND discount_percent <= 90",
    )
    op.create_index("ix_menu_items_is_surplus_deal", "menu_items", ["is_surplus_deal"])

    op.add_column(
        "order_items",
        sa.Column(
            "original_unit_price_paise",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "order_items",
        sa.Column(
            "discount_percent_applied",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute("UPDATE order_items SET original_unit_price_paise = unit_price_paise")


def downgrade() -> None:
    op.drop_column("order_items", "discount_percent_applied")
    op.drop_column("order_items", "original_unit_price_paise")
    op.drop_index("ix_menu_items_is_surplus_deal", table_name="menu_items")
    op.drop_constraint(
        "ck_menu_items_discount_percent_range", "menu_items", type_="check"
    )
    op.drop_column("menu_items", "surplus_expires_at")
    op.drop_column("menu_items", "surplus_note")
    op.drop_column("menu_items", "is_surplus_deal")
    op.drop_column("menu_items", "discount_percent")
