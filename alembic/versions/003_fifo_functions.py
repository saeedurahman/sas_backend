"""FIFO costing function get_fifo_cost.

Revision ID: 003_fifo_functions
Revises: 002_seed_permissions
Create Date: 2026-06-02

Note: Also included in schema/013_fifo_functions.sql for psql installs.
      This migration ensures Alembic-only deploys get the function after seeds.

"""

from typing import Sequence, Union

from alembic import op

from migration_utils import SCHEMA_DIR, run_sql_file

revision: str = "003_fifo_functions"
down_revision: Union[str, Sequence[str], None] = "002_seed_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    run_sql_file(op.get_bind(), SCHEMA_DIR / "013_fifo_functions.sql")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS get_fifo_cost(UUID, UUID, UUID, NUMERIC)")
