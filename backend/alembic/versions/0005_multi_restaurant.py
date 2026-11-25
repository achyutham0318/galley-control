from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_multi_restaurant"
down_revision: str | None = "0004_surplus_discounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_RESTAURANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute("ALTER TYPE audit_event ADD VALUE IF NOT EXISTS 'MENU_ITEM_CREATED'")
    op.execute("ALTER TYPE audit_event ADD VALUE IF NOT EXISTS 'MENU_ITEM_UPDATED'")
    op.execute("ALTER TYPE audit_event ADD VALUE IF NOT EXISTS 'MENU_ITEM_ARCHIVED'")

    op.create_table(
        "restaurants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("location_tag", sa.String(length=120), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_restaurants_slug", "restaurants", ["slug"], unique=True)

    op.execute(
        f"""
        INSERT INTO restaurants (id, name, slug, description, location_tag, is_active)
        VALUES (
            '{_DEFAULT_RESTAURANT_ID}',
            'Main Campus Canteen',
            'main-canteen',
            'Primary campus food court canteen',
            'Central Food Court',
            true
        )
        ON CONFLICT (slug) DO NOTHING
        """
    )

    op.add_column(
        "users",
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_restaurant_id",
        "users",
        "restaurants",
        ["restaurant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_users_restaurant_id", "users", ["restaurant_id"])
    op.execute(
        f"UPDATE users SET restaurant_id = '{_DEFAULT_RESTAURANT_ID}' WHERE role IN ('STAFF', 'MANAGER')"
    )

    op.add_column(
        "menu_items",
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "menu_items",
        sa.Column(
            "category",
            sa.String(length=60),
            nullable=False,
            server_default=sa.text("'General'"),
        ),
    )
    op.add_column(
        "menu_items",
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        f"UPDATE menu_items SET restaurant_id = '{_DEFAULT_RESTAURANT_ID}' WHERE restaurant_id IS NULL"
    )
    op.alter_column("menu_items", "restaurant_id", nullable=False)
    op.create_foreign_key(
        "fk_menu_items_restaurant_id",
        "menu_items",
        "restaurants",
        ["restaurant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_menu_items_restaurant_id", "menu_items", ["restaurant_id"])
    op.create_index("ix_menu_items_is_archived", "menu_items", ["is_archived"])
    op.drop_constraint("uq_menu_items_name", "menu_items", type_="unique")
    op.create_unique_constraint(
        "uq_menu_items_restaurant_name", "menu_items", ["restaurant_id", "name"]
    )

    op.add_column(
        "orders",
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        f"UPDATE orders SET restaurant_id = '{_DEFAULT_RESTAURANT_ID}' WHERE restaurant_id IS NULL"
    )
    op.alter_column("orders", "restaurant_id", nullable=False)
    op.create_foreign_key(
        "fk_orders_restaurant_id",
        "orders",
        "restaurants",
        ["restaurant_id"],
        ["id"],
    )
    op.create_index("ix_orders_restaurant_id", "orders", ["restaurant_id"])

    op.add_column(
        "refunds",
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_refunds_restaurant_id",
        "refunds",
        "restaurants",
        ["restaurant_id"],
        ["id"],
    )
    op.create_index("ix_refunds_restaurant_id", "refunds", ["restaurant_id"])
    op.execute(
        f"UPDATE refunds SET restaurant_id = '{_DEFAULT_RESTAURANT_ID}' WHERE restaurant_id IS NULL"
    )

    op.add_column(
        "audit_log",
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_audit_log_restaurant_id",
        "audit_log",
        "restaurants",
        ["restaurant_id"],
        ["id"],
    )
    op.create_index("ix_audit_log_restaurant_id", "audit_log", ["restaurant_id"])
    op.execute(
        f"UPDATE audit_log SET restaurant_id = '{_DEFAULT_RESTAURANT_ID}' WHERE restaurant_id IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_restaurant_id", table_name="audit_log")
    op.drop_constraint("fk_audit_log_restaurant_id", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "restaurant_id")

    op.drop_index("ix_refunds_restaurant_id", table_name="refunds")
    op.drop_constraint("fk_refunds_restaurant_id", "refunds", type_="foreignkey")
    op.drop_column("refunds", "restaurant_id")

    op.drop_index("ix_orders_restaurant_id", table_name="orders")
    op.drop_constraint("fk_orders_restaurant_id", "orders", type_="foreignkey")
    op.drop_column("orders", "restaurant_id")

    op.drop_constraint("uq_menu_items_restaurant_name", "menu_items", type_="unique")
    op.create_unique_constraint("uq_menu_items_name", "menu_items", ["name"])
    op.drop_index("ix_menu_items_is_archived", table_name="menu_items")
    op.drop_index("ix_menu_items_restaurant_id", table_name="menu_items")
    op.drop_constraint("fk_menu_items_restaurant_id", "menu_items", type_="foreignkey")
    op.drop_column("menu_items", "is_archived")
    op.drop_column("menu_items", "category")
    op.drop_column("menu_items", "restaurant_id")

    op.drop_index("ix_users_restaurant_id", table_name="users")
    op.drop_constraint("fk_users_restaurant_id", "users", type_="foreignkey")
    op.drop_column("users", "restaurant_id")

    op.drop_index("ix_restaurants_slug", table_name="restaurants")
    op.drop_table("restaurants")
