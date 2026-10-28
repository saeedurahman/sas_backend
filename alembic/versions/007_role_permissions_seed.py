"""Backfill default role_permissions for all existing businesses.

Revision ID: 007_role_permissions_seed
Revises: 006_full_permissions_seed
Create Date: 2026-06-28

"""

from typing import Sequence, Union

from alembic import op

from app.services.role_permission_seed import backfill_role_permissions_for_all_businesses

revision: str = "007_role_permissions_seed"
down_revision: Union[str, Sequence[str], None] = "006_full_permissions_seed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    backfill_role_permissions_for_all_businesses(op.get_bind(), dry_run=False)


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions rp
        USING roles r
        WHERE rp.role_id = r.id
          AND rp.business_id = r.business_id
          AND lower(r.name) IN ('owner', 'manager', 'cashier')
          AND r.is_system = TRUE
        """
    )
