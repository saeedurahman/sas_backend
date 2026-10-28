"""sale_return_lines.discount_amount: refunds honour the original line discount.

Revision ID: 017_return_line_discount
Revises: 016_money_inventory_correctness
Create Date: 2026-10-28

"""

from typing import Sequence, Union

from alembic import op

revision: str = "017_return_line_discount"
down_revision: Union[str, Sequence[str], None] = "016_money_inventory_correctness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sale_return_lines
            ADD COLUMN IF NOT EXISTS discount_amount NUMERIC(15,2) NOT NULL DEFAULT 0;
        ALTER TABLE sale_return_lines
            ADD CONSTRAINT chk_sale_return_lines_discount CHECK (discount_amount >= 0);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE sale_return_lines DROP CONSTRAINT IF EXISTS chk_sale_return_lines_discount;
        ALTER TABLE sale_return_lines DROP COLUMN IF EXISTS discount_amount;
        """
    )
