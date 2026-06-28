"""Add discounts.view permission and backfill role_permissions.

Revision ID: 008_discounts_view_permission
Revises: 007_role_permissions_seed
Create Date: 2026-06-28

"""

from typing import Sequence, Union

from alembic import op

from app.services.role_permission_seed import backfill_role_permissions_for_all_businesses
from migration_utils import SEEDS_DIR, run_sql_file

revision: str = "008_discounts_view_permission"
down_revision: Union[str, Sequence[str], None] = "007_role_permissions_seed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    run_sql_file(op.get_bind(), SEEDS_DIR / "003_discounts_view_permission.sql")
    backfill_role_permissions_for_all_businesses(op.get_bind(), dry_run=False)


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions rp
        USING permissions p
        WHERE rp.permission_id = p.id
          AND p.permission_key = 'discounts.view'
        """
    )
    op.execute("DELETE FROM permissions WHERE permission_key = 'discounts.view'")
