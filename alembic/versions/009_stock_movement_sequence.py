"""Add movement_sequence to stock_movements for deterministic FIFO ordering.

Revision ID: 009_stock_movement_sequence
Revises: 008_discounts_view_permission
Create Date: 2026-06-29

"""

from typing import Sequence, Union

from alembic import op

revision: str = "009_stock_movement_sequence"
down_revision: Union[str, Sequence[str], None] = "008_discounts_view_permission"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE stock_movements
            ADD COLUMN IF NOT EXISTS movement_sequence SMALLINT;

        COMMENT ON COLUMN stock_movements.movement_sequence IS
            'Per sale-line consumption order (0-based). NULL for historical rows.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE stock_movements
            DROP COLUMN IF EXISTS movement_sequence;
        """
    )
