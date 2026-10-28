"""Allow purchase_lines sourced from production orders (manufacturing FIFO IN).

Revision ID: 012_mfg_purchase_lines
Revises: 011_restaurant_permissions
Create Date: 2026-06-30

"""

from typing import Sequence, Union

from alembic import op

revision: str = "012_mfg_purchase_lines"
down_revision: Union[str, Sequence[str], None] = "011_restaurant_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE purchase_lines
            ALTER COLUMN purchase_order_id DROP NOT NULL;

        ALTER TABLE purchase_lines
            ADD COLUMN IF NOT EXISTS production_order_id UUID
                REFERENCES production_orders(id) ON DELETE SET NULL;

        CREATE INDEX IF NOT EXISTS idx_purchase_lines_production
            ON purchase_lines (business_id, production_order_id)
            WHERE production_order_id IS NOT NULL AND deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_purchase_lines_production;

        ALTER TABLE purchase_lines
            DROP COLUMN IF EXISTS production_order_id;

        DELETE FROM purchase_lines WHERE purchase_order_id IS NULL;

        ALTER TABLE purchase_lines
            ALTER COLUMN purchase_order_id SET NOT NULL;
        """
    )
