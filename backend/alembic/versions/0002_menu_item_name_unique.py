from collections.abc import Sequence

from alembic import op

revision: str = "0002_menu_item_name_unique"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_menu_items_name", "menu_items", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_menu_items_name", "menu_items", type_="unique")
