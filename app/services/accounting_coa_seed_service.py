"""Default chart of accounts seeding when accounting is enabled."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import ChartOfAccount

DEFAULT_CHART_ACCOUNTS: tuple[dict[str, str | None], ...] = (
    {
        "account_code": "1000",
        "account_name": "Cash on Hand",
        "account_type": "asset",
        "account_subtype": "cash",
    },
    {
        "account_code": "1010",
        "account_name": "Bank",
        "account_type": "asset",
        "account_subtype": "bank",
    },
    {
        "account_code": "1100",
        "account_name": "Accounts Receivable",
        "account_type": "asset",
        "account_subtype": "accounts_receivable",
    },
    {
        "account_code": "1200",
        "account_name": "Inventory",
        "account_type": "asset",
        "account_subtype": "inventory",
    },
    {
        "account_code": "2000",
        "account_name": "Accounts Payable",
        "account_type": "liability",
        "account_subtype": "accounts_payable",
    },
    {
        "account_code": "2100",
        "account_name": "Sales Tax Payable",
        "account_type": "liability",
        "account_subtype": "tax_payable",
    },
    {
        "account_code": "3000",
        "account_name": "Owner's Capital",
        "account_type": "equity",
        "account_subtype": "other",
    },
    {
        "account_code": "3100",
        "account_name": "Owner's Drawings",
        "account_type": "equity",
        "account_subtype": "other",
    },
    {
        "account_code": "4000",
        "account_name": "Sales Revenue",
        "account_type": "income",
        "account_subtype": "sales_revenue",
    },
    {
        "account_code": "5000",
        "account_name": "Cost of Goods Sold",
        "account_type": "expense",
        "account_subtype": "cogs",
    },
    {
        "account_code": "6000",
        "account_name": "Operating Expenses",
        "account_type": "expense",
        "account_subtype": "other",
    },
    {
        "account_code": "6100",
        "account_name": "Rent Expense",
        "account_type": "expense",
        "account_subtype": "other",
    },
    {
        "account_code": "6200",
        "account_name": "Utilities Expense",
        "account_type": "expense",
        "account_subtype": "other",
    },
    {
        "account_code": "6300",
        "account_name": "Salaries & Wages",
        "account_type": "expense",
        "account_subtype": "other",
    },
)


async def chart_of_accounts_exists(db: AsyncSession, business_id: UUID) -> bool:
    result = await db.execute(
        select(func.count())
        .select_from(ChartOfAccount)
        .where(
            ChartOfAccount.business_id == business_id,
            ChartOfAccount.deleted_at.is_(None),
        )
    )
    return int(result.scalar_one()) > 0


async def seed_default_chart_of_accounts(
    db: AsyncSession,
    business_id: UUID,
    *,
    created_by: UUID | None = None,
) -> int:
    """Insert the 14 system accounts if the tenant chart is empty. Returns rows inserted."""
    if await chart_of_accounts_exists(db, business_id):
        return 0

    now = datetime.now(timezone.utc)
    inserted = 0
    for row in DEFAULT_CHART_ACCOUNTS:
        db.add(
            ChartOfAccount(
                business_id=business_id,
                parent_id=None,
                account_code=str(row["account_code"]),
                account_name=str(row["account_name"]),
                account_type=str(row["account_type"]),
                account_subtype=row["account_subtype"],
                is_system=True,
                is_active=True,
                created_by=created_by,
                updated_by=created_by,
                created_at=now,
                updated_at=now,
            )
        )
        inserted += 1
    if inserted:
        await db.flush()
    return inserted


async def ensure_default_chart_of_accounts(
    db: AsyncSession,
    business_id: UUID,
    *,
    created_by: UUID | None = None,
) -> int:
    """Idempotent wrapper used by list endpoint when accounting is enabled."""
    return await seed_default_chart_of_accounts(
        db,
        business_id,
        created_by=created_by,
    )
