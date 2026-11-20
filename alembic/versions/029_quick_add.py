"""Quick Add by scan: products.pack_size and products.mrp.

Both are optional catalog facts a shop types (or, later, the master catalogue supplies) while
scanning stock in:

- ``products.pack_size``  free text such as "500 g" / "6 x 200 ml" (display only).
- ``products.mrp``        printed maximum retail price (display / reference only; the selling price
                          stays in the price lists).

Plain nullable columns: Postgres adds them without rewriting rows, so existing products are not
touched and the sync change feed is not churned. The existing product change trigger (migration 019,
"every data column") stamps new values like any other product edit. No new index is needed: the
barcode lookup uses the existing ``uq_barcodes_business_code`` / ``idx_barcodes_lookup``.

Idempotent: every step checks for existing objects first.

Revision ID: 029_quick_add
Revises: 028_receipt_language
Create Date: 2026-11-21
"""

from typing import Sequence, Union

from alembic import op

revision: str = "029_quick_add"
down_revision: Union[str, Sequence[str], None] = "028_receipt_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS pack_size VARCHAR(50)")
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS mrp NUMERIC(15,2)")


def downgrade() -> None:
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS mrp")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS pack_size")
