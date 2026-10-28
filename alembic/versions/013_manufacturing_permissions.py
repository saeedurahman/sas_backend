"""Add manufacturing.* permissions and backfill role_permissions.

Revision ID: 013_mfg_permissions
Revises: 012_mfg_purchase_lines
Create Date: 2026-06-30

"""

from typing import Sequence, Union

from alembic import op

from app.services.role_permission_seed import backfill_role_permissions_for_all_businesses
from migration_utils import SEEDS_DIR, run_sql_file

revision: str = "013_mfg_permissions"
down_revision: Union[str, Sequence[str], None] = "012_mfg_purchase_lines"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MANUFACTURING_KEYS = (
    "manufacturing.bom.view",
    "manufacturing.bom.manage",
    "manufacturing.production.view",
    "manufacturing.production.create",
    "manufacturing.production.complete",
    "manufacturing.production.cancel",
)


def upgrade() -> None:
    run_sql_file(op.get_bind(), SEEDS_DIR / "005_manufacturing_permissions.sql")
    backfill_role_permissions_for_all_businesses(op.get_bind(), dry_run=False)


def downgrade() -> None:
    keys_sql = ", ".join(f"'{key}'" for key in _MANUFACTURING_KEYS)
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
