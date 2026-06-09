"""Seed default permission keys.

Revision ID: 002_seed_permissions
Revises: 001_initial_schema
Create Date: 2026-06-02

"""

from typing import Sequence, Union

from alembic import op

from migration_utils import SEEDS_DIR, run_sql_file

revision: str = "002_seed_permissions"
down_revision: Union[str, Sequence[str], None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    run_sql_file(op.get_bind(), SEEDS_DIR / "001_permissions.sql")


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM permissions
        WHERE permission_key IN (
            'sales.create', 'sales.view',
            'inventory.adjust', 'inventory.view',
            'products.manage', 'reports.view',
            'settings.manage', 'users.manage'
        );
        """
    )
