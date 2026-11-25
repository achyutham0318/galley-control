from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role_enum = postgresql.ENUM(
    "STUDENT", "STAFF", "MANAGER", name="user_role", create_type=False
)
order_status_enum = postgresql.ENUM(
    "CREATED",
    "PAID",
    "COLLECTED",
    "PENDING_REVIEW",
    "REFUNDED",
    "CANCELLED",
    name="order_status",
    create_type=False,
)
prep_stage_enum = postgresql.ENUM(
    "RECEIVED", "IN_PREP", "READY", name="prep_stage", create_type=False
)
refund_decision_enum = postgresql.ENUM(
    "AUTO_APPROVE",
    "ESCALATE",
    "BLOCK",
    "MANAGER_APPROVE",
    "MANAGER_REJECT",
    name="refund_decision",
    create_type=False,
)
refund_status_enum = postgresql.ENUM(
    "PENDING_REVIEW",
    "INITIATED",
    "COMPLETED",
    "REJECTED",
    "BLOCKED",
    "FAILED",
    name="refund_status",
    create_type=False,
)
audit_tier_enum = postgresql.ENUM(
    "TIER_1", "TIER_2", "HUMAN_OVERRIDE", name="audit_tier", create_type=False
)
audit_event_enum = postgresql.ENUM(
    "QR_VERIFY",
    "QR_REFRESH",
    "REFUND_EVAL",
    "MANAGER_DECISION",
    "STAGE_CHANGE",
    "MANUAL_COLLECT",
    "PAYMENT_EVENT",
    "OFFLINE_SYNC_CONFLICT",
    name="audit_event",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    user_role_enum.create(bind, checkfirst=True)
    order_status_enum.create(bind, checkfirst=True)
    prep_stage_enum.create(bind, checkfirst=True)
    refund_decision_enum.create(bind, checkfirst=True)
    refund_status_enum.create(bind, checkfirst=True)
    audit_tier_enum.create(bind, checkfirst=True)
    audit_event_enum.create(bind, checkfirst=True)

    op.execute("CREATE SEQUENCE IF NOT EXISTS order_number_seq START WITH 1001 INCREMENT BY 1")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("staff_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role", user_role_enum, nullable=False),
        sa.Column("pin_hash", sa.String(length=255), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "failed_pin_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "(role = 'STUDENT' AND email IS NOT NULL AND password_hash IS NOT NULL) "
            "OR (role IN ('STAFF', 'MANAGER') AND pin_hash IS NOT NULL "
            "AND (staff_id IS NOT NULL OR email IS NOT NULL))",
            name="ck_users_role_credentials",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("staff_id"),
    )

    op.create_table(
        "menu_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("price_paise", sa.Integer(), nullable=False),
        sa.Column("stock_count", sa.Integer(), nullable=False),
        sa.Column("estimated_prep_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "is_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("price_paise > 0", name="ck_menu_items_price_positive"),
        sa.CheckConstraint("stock_count >= 0", name="ck_menu_items_stock_non_negative"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "order_number",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'GC-' || nextval('order_number_seq')::text"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            order_status_enum,
            nullable=False,
            server_default=sa.text("'CREATED'"),
        ),
        sa.Column(
            "prep_stage",
            prep_stage_enum,
            nullable=False,
            server_default=sa.text("'RECEIVED'"),
        ),
        sa.Column("qr_nonce", sa.String(length=128), nullable=False),
        sa.Column("qr_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qr_redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verified_via_scan",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("razorpay_order_id", sa.String(length=128), nullable=True),
        sa.Column("razorpay_payment_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_prep_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_number"),
        sa.UniqueConstraint("qr_nonce"),
        sa.UniqueConstraint("razorpay_order_id"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_orders_user_idempotency_key",
        ),
    )
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
    op.create_index("ix_orders_status_prep_stage", "orders", ["status", "prep_stage"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])

    op.create_table(
        "order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("menu_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_name_snapshot", sa.String(length=120), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_paise", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.ForeignKeyConstraint(["menu_item_id"], ["menu_items.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prep_stage_at_request", prep_stage_enum, nullable=False),
        sa.Column("refund_count_7d", sa.Integer(), nullable=False),
        sa.Column("surge_active", sa.Boolean(), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("factor_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decision", refund_decision_enum, nullable=False),
        sa.Column("status", refund_status_enum, nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("human_message", sa.Text(), nullable=False),
        sa.Column("razorpay_refund_id", sa.String(length=128), nullable=True),
        sa.Column("manager_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manager_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index("ix_refunds_user_id", "refunds", ["user_id"])
    op.create_index("ix_refunds_created_at", "refunds", ["created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tier", audit_tier_enum, nullable=False),
        sa.Column("event_type", audit_event_enum, nullable=False),
        sa.Column("input_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("human_message", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_order_id", "audit_log", ["order_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_log_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only; UPDATE and DELETE are prohibited';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW
        EXECUTE FUNCTION prevent_audit_log_mutation();
        """
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_event_id"),
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_log_mutation()")

    op.drop_table("webhook_events")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_order_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_refunds_created_at", table_name="refunds")
    op.drop_index("ix_refunds_user_id", table_name="refunds")
    op.drop_table("refunds")
    op.drop_table("order_items")
    op.drop_index("ix_orders_created_at", table_name="orders")
    op.drop_index("ix_orders_status_prep_stage", table_name="orders")
    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_table("orders")
    op.drop_table("menu_items")
    op.drop_table("users")

    op.execute("DROP SEQUENCE IF EXISTS order_number_seq")

    bind = op.get_bind()
    audit_event_enum.drop(bind, checkfirst=True)
    audit_tier_enum.drop(bind, checkfirst=True)
    refund_status_enum.drop(bind, checkfirst=True)
    refund_decision_enum.drop(bind, checkfirst=True)
    prep_stage_enum.drop(bind, checkfirst=True)
    order_status_enum.drop(bind, checkfirst=True)
    user_role_enum.drop(bind, checkfirst=True)
