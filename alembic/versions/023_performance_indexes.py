"""Performance indexes for analytics, trigram search, and high-volume queries.

Adds pg_trgm extension and GIN trigram indexes for fast ILIKE searches on
products, barcodes, customers, suppliers, sales, and expenses.
Adds partial composite B-tree indexes for analytics date ranges and status filters.

Revision ID: 023_performance_indexes
Revises: 022_export_performance_indexes
Create Date: 2026-11-15
"""

from typing import Sequence, Union
from alembic import op

revision: str = "023_performance_indexes"
down_revision: Union[str, Sequence[str], None] = "022_export_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TRGM_INDEXES = [
    ("idx_products_name_trgm", "products", "USING gin (name gin_trgm_ops)"),
    ("idx_products_sku_trgm", "products", "USING gin (sku gin_trgm_ops)"),
    ("idx_barcodes_barcode_trgm", "barcodes", "USING gin (barcode gin_trgm_ops)"),
    ("idx_customers_name_trgm", "customers", "USING gin (name gin_trgm_ops)"),
    ("idx_customers_phone_trgm", "customers", "USING gin (phone gin_trgm_ops)"),
    ("idx_suppliers_name_trgm", "suppliers", "USING gin (name gin_trgm_ops)"),
    ("idx_sales_sale_number_trgm", "sales", "USING gin (sale_number gin_trgm_ops)"),
    ("idx_sales_client_receipt_trgm", "sales", "USING gin (client_receipt_no gin_trgm_ops) WHERE client_receipt_no IS NOT NULL"),
    ("idx_expenses_number_trgm", "expenses", "USING gin (expense_number gin_trgm_ops)"),
    ("idx_expenses_desc_trgm", "expenses", "USING gin (description gin_trgm_ops) WHERE description IS NOT NULL"),
]

_ANALYTICS_INDEXES = [
    (
        "idx_sales_analytics_date",
        "sales",
        "(business_id, sold_at) WHERE deleted_at IS NULL AND status NOT IN ('cancelled', 'voided')",
    ),
    (
        "idx_sales_analytics_branch_date",
        "sales",
        "(business_id, branch_id, sold_at) WHERE deleted_at IS NULL AND status NOT IN ('cancelled', 'voided')",
    ),
    (
        "idx_expenses_analytics_date",
        "expenses",
        "(business_id, expense_date) WHERE deleted_at IS NULL",
    ),
    (
        "idx_expenses_analytics_branch",
        "expenses",
        "(business_id, branch_id, expense_date) WHERE deleted_at IS NULL",
    ),
    (
        "idx_sale_returns_analytics",
        "sale_returns",
        "(business_id, returned_at) WHERE deleted_at IS NULL",
    ),
    (
        "idx_sale_returns_branch_analytics",
        "sale_returns",
        "(business_id, branch_id, returned_at) WHERE deleted_at IS NULL",
    ),
    (
        "idx_shifts_open_analytics",
        "register_shifts",
        "(business_id, status) WHERE deleted_at IS NULL AND status = 'open'",
    ),
]


def upgrade() -> None:
    # Ensure pg_trgm extension is available
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    for name, table, cols in _TRGM_INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}")

    for name, table, cols in _ANALYTICS_INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}")


def downgrade() -> None:
    for name, _, _ in reversed(_ANALYTICS_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")

    for name, _, _ in reversed(_TRGM_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
