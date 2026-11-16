"""Tax and price settings: per-sale snapshots, stored net amounts, tax-exempt flag.

- sales / sale_returns: tax_inclusive, tax_rounding_mode, rounding_amount, tax_label.
  Added with constant server defaults (false / 'none' / 0 / 'GST'), which Postgres applies to
  existing rows without rewriting them, so existing sales never change and the sync change
  feed is not churned by a backfill UPDATE.
- sale_lines / sale_return_lines: net_amount (line amount before tax), backfilled from the
  existing exclusive data as ROUND(qty * unit_price, 2) - discount_amount.
- products.is_tax_exempt (default false).
- Settings changes (app_settings) already stamp business_configs via trg_app_settings_sync
  (migration 025, any key), so the new tax.* keys reach devices with no trigger change.

Idempotent: every step checks for existing objects first.

Revision ID: 026_tax_price_settings
Revises: 025_offline_sync_gaps
Create Date: 2026-11-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "026_tax_price_settings"
down_revision: Union[str, Sequence[str], None] = "025_offline_sync_gaps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SNAPSHOT_TABLES = ("sales", "sale_returns")


def upgrade() -> None:
    for table in _SNAPSHOT_TABLES:
        op.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS tax_inclusive BOOLEAN NOT NULL DEFAULT FALSE, "
            "ADD COLUMN IF NOT EXISTS tax_rounding_mode TEXT NOT NULL DEFAULT 'none', "
            "ADD COLUMN IF NOT EXISTS rounding_amount NUMERIC(12,2) NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS tax_label TEXT NOT NULL DEFAULT 'GST'"
        )

    for table in ("sale_lines", "sale_return_lines"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS net_amount NUMERIC(15,2)"
        )
        op.execute(
            f"UPDATE {table} SET net_amount = ROUND(qty * unit_price, 2) - discount_amount "
            "WHERE net_amount IS NULL"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN net_amount SET DEFAULT 0")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN net_amount SET NOT NULL")

    op.execute(
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_tax_exempt BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # Tax breakdown reports group by label + rate.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_tax_label ON sales (business_id, tax_label) "
        "WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sales_tax_label")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS is_tax_exempt")
    for table in ("sale_lines", "sale_return_lines"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS net_amount")
    for table in _SNAPSHOT_TABLES:
        op.execute(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS tax_inclusive, "
            "DROP COLUMN IF EXISTS tax_rounding_mode, DROP COLUMN IF EXISTS rounding_amount, "
            "DROP COLUMN IF EXISTS tax_label"
        )
