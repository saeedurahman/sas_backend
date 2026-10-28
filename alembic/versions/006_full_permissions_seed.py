"""Seed full permission catalog.

Revision ID: 006_full_permissions_seed
Revises: 005_auth_security
Create Date: 2026-06-02

"""

from typing import Sequence, Union

from alembic import op

from migration_utils import SEEDS_DIR, run_sql_file

revision: str = "006_full_permissions_seed"
down_revision: Union[str, Sequence[str], None] = "005_auth_security"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FULL_PERMISSION_KEYS = (
    "auth.login",
    "auth.logout",
    "auth.refresh",
    "products.view",
    "products.create",
    "products.update",
    "products.delete",
    "products.manage_categories",
    "products.manage_brands",
    "products.manage_units",
    "products.manage_prices",
    "products.manage_barcodes",
    "inventory.view",
    "inventory.adjust",
    "inventory.purchase_orders.view",
    "inventory.purchase_orders.create",
    "inventory.purchase_orders.receive",
    "inventory.transfers.view",
    "inventory.transfers.create",
    "inventory.transfers.receive",
    "inventory.waste.view",
    "inventory.waste.create",
    "sales.view",
    "sales.create",
    "sales.cancel",
    "sales.apply_discount",
    "sales.override_price",
    "sales.returns.view",
    "sales.returns.create",
    "sales.payments.view",
    "customers.view",
    "customers.create",
    "customers.update",
    "customers.ledger.view",
    "suppliers.view",
    "suppliers.create",
    "suppliers.update",
    "suppliers.ledger.view",
    "suppliers.ledger.payment",
    "expenses.view",
    "expenses.create",
    "expenses.update",
    "expenses.delete",
    "expenses.categories.manage",
    "registers.view",
    "registers.manage",
    "shifts.view",
    "shifts.open",
    "shifts.close",
    "shifts.cash_movement",
    "reports.view",
    "reports.sales",
    "reports.inventory",
    "reports.financial",
    "reports.analytics",
    "reports.export",
    "reports.fraud_alerts",
    "settings.view",
    "settings.manage",
    "users.view",
    "users.create",
    "users.update",
    "users.delete",
    "users.roles.manage",
    "notifications.view",
    "notifications.manage",
)


def upgrade() -> None:
    run_sql_file(op.get_bind(), SEEDS_DIR / "002_full_permissions.sql")


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k in _FULL_PERMISSION_KEYS)
    op.execute(f"DELETE FROM permissions WHERE permission_key IN ({keys});")
