"""Add restaurant.* permissions and backfill role_permissions.

Revision ID: 011_restaurant_permissions
Revises: 010_document_number_counters
Create Date: 2026-06-30

"""

from typing import Sequence, Union

from alembic import op

from app.services.role_permission_seed import backfill_role_permissions_for_all_businesses
from migration_utils import SEEDS_DIR, run_sql_file

revision: str = "011_restaurant_permissions"
down_revision: Union[str, Sequence[str], None] = "010_document_number_counters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RESTAURANT_KEYS = (
    "restaurant.floor_plans.view",
    "restaurant.floor_plans.manage",
    "restaurant.tables.view",
    "restaurant.tables.manage",
    "restaurant.tables.update_status",
    "restaurant.modifiers.view",
    "restaurant.modifiers.manage",
    "restaurant.kot.view",
    "restaurant.kot.update_status",
    "restaurant.kot.fire",
)


def upgrade() -> None:
    run_sql_file(op.get_bind(), SEEDS_DIR / "004_restaurant_permissions.sql")
    backfill_role_permissions_for_all_businesses(op.get_bind(), dry_run=False)


def downgrade() -> None:
    keys_sql = ", ".join(f"'{key}'" for key in _RESTAURANT_KEYS)
    op.execute(
        f"""
        DELETE FROM role_permissions rp
        USING permissions p
        WHERE rp.permission_id = p.id
          AND p.permission_key IN ({keys_sql})
        """
    )
    op.execute(
        f"DELETE FROM permissions WHERE permission_key IN ({keys_sql})"
    )
