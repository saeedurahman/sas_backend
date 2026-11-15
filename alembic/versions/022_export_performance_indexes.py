"""Export performance indexes for reporting and data downloads.

Adds composite covering and partial indexes on sales, sale_lines, sale_payments,
expenses, purchase_orders, purchase_lines, customer_ledger, supplier_ledger,
barcodes, and stock_levels to eliminate full table scans, in-memory sorts,
and sequential scans in lateral export queries.

Revision ID: 022_export_performance_indexes
Revises: 021_phase5_support
Create Date: 2026-11-14
"""

from typing import Sequence, Union
from alembic import op

revision: str = "022_export_performance_indexes"
down_revision: Union[str, Sequence[str], None] = "021_phase5_support"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = [
    (
        "idx_sales_export_date",
        "sales",
        "(business_id, sold_at, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_sales_branch_export_date",
        "sales",
        "(business_id, branch_id, sold_at, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_sale_lines_export",
        "sale_lines",
        "(business_id, sale_id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_sale_payments_export",
        "sale_payments",
        "(business_id, sale_id) WHERE deleted_at IS NULL AND status = 'completed'",
    ),
    (
        "idx_expenses_export_date",
        "expenses",
        "(business_id, expense_date, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_expenses_branch_export_date",
        "expenses",
        "(business_id, branch_id, expense_date, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_purchase_orders_export_date",
        "purchase_orders",
        "(business_id, ordered_at, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_purchase_orders_branch_export_date",
        "purchase_orders",
        "(business_id, branch_id, ordered_at, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_purchase_lines_export",
        "purchase_lines",
        "(business_id, purchase_order_id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_customer_ledger_export_date",
        "customer_ledger",
        "(business_id, entry_at, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_customer_ledger_customer_bal",
        "customer_ledger",
        "(business_id, customer_id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_supplier_ledger_export_date",
        "supplier_ledger",
        "(business_id, entry_at, id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_supplier_ledger_supplier_bal",
        "supplier_ledger",
        "(business_id, supplier_id) WHERE deleted_at IS NULL",
    ),
    (
        "idx_barcodes_product_export",
        "barcodes",
        "(business_id, product_id, is_primary DESC, created_at) WHERE deleted_at IS NULL",
    ),
    (
        "idx_stock_levels_product_export",
        "stock_levels",
        "(business_id, product_id) WHERE deleted_at IS NULL",
    ),
]


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}")


def downgrade() -> None:
    for name, _, _ in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
