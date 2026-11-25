from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_wallet_and_loyalty"
down_revision: str | None = "0002_menu_item_name_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    wallet_tx_enum = postgresql.ENUM(
        "POINTS_EARNED_PICKUP",
        "POINTS_CONVERTED_TO_COINS",
        "REFUND_CREDITED_COINS",
        "ORDER_SPENT_COINS",
        name="wallet_tx_type",
    )
    wallet_tx_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "student_wallets",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "loyalty_points",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "gold_coins",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "lifetime_points_earned",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "lifetime_coins_earned",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "loyalty_points >= 0",
            name="ck_student_wallets_loyalty_points_non_negative",
        ),
        sa.CheckConstraint(
            "gold_coins >= 0",
            name="ck_student_wallets_gold_coins_non_negative",
        ),
    )

    op.add_column(
        "orders",
        sa.Column(
            "coins_redeemed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "coin_discount_paise",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "cash_payable_paise",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "refund_coins_credited",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute("UPDATE orders SET cash_payable_paise = total_paise")
    op.create_check_constraint(
        "ck_orders_coins_redeemed_non_negative", "orders", "coins_redeemed >= 0"
    )
    op.create_check_constraint(
        "ck_orders_coin_discount_non_negative", "orders", "coin_discount_paise >= 0"
    )
    op.create_check_constraint(
        "ck_orders_cash_payable_non_negative", "orders", "cash_payable_paise >= 0"
    )
    op.create_check_constraint(
        "ck_orders_refund_coins_credited_non_negative",
        "orders",
        "refund_coins_credited >= 0",
    )

    op.create_table(
        "wallet_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id"),
            nullable=True,
        ),
        sa.Column(
            "tx_type",
            postgresql.ENUM(
                "POINTS_EARNED_PICKUP",
                "POINTS_CONVERTED_TO_COINS",
                "REFUND_CREDITED_COINS",
                "ORDER_SPENT_COINS",
                name="wallet_tx_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "points_delta",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "coins_delta",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("balance_points_after", sa.Integer(), nullable=False),
        sa.Column("balance_coins_after", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_wallet_tx_idempotency_key"),
        sa.CheckConstraint(
            "balance_points_after >= 0",
            name="ck_wallet_tx_balance_points_non_negative",
        ),
        sa.CheckConstraint(
            "balance_coins_after >= 0",
            name="ck_wallet_tx_balance_coins_non_negative",
        ),
    )
    op.create_index("ix_wallet_tx_user_id", "wallet_transactions", ["user_id"])
    op.create_index("ix_wallet_tx_order_id", "wallet_transactions", ["order_id"])
    op.create_index(
        "ix_wallet_tx_idempotency_key",
        "wallet_transactions",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_wallet_tx_created_at", "wallet_transactions", ["created_at"])
    op.create_index(
        "ix_wallet_tx_user_created", "wallet_transactions", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_tx_user_created", table_name="wallet_transactions")
    op.drop_index("ix_wallet_tx_created_at", table_name="wallet_transactions")
    op.drop_index("ix_wallet_tx_idempotency_key", table_name="wallet_transactions")
    op.drop_index("ix_wallet_tx_order_id", table_name="wallet_transactions")
    op.drop_index("ix_wallet_tx_user_id", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
    op.drop_constraint(
        "ck_orders_refund_coins_credited_non_negative", "orders", type_="check"
    )
    op.drop_constraint("ck_orders_cash_payable_non_negative", "orders", type_="check")
    op.drop_constraint("ck_orders_coin_discount_non_negative", "orders", type_="check")
    op.drop_constraint("ck_orders_coins_redeemed_non_negative", "orders", type_="check")
    op.drop_column("orders", "refund_coins_credited")
    op.drop_column("orders", "cash_payable_paise")
    op.drop_column("orders", "coin_discount_paise")
    op.drop_column("orders", "coins_redeemed")
    op.drop_table("student_wallets")
    postgresql.ENUM(name="wallet_tx_type").drop(op.get_bind(), checkfirst=True)
