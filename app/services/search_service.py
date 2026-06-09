"""Global search across products, customers, suppliers, sales, and expenses."""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.search import GlobalSearchResponse, GlobalSearchResult

_ENTITY_ORDER = {
    "product": 0,
    "customer": 1,
    "supplier": 2,
    "sale": 3,
    "expense": 4,
}
_PER_ENTITY_LIMIT = 5


def _meta_value(value: Decimal | datetime | date | int | str | bool | None) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _is_exact_match(query: str, *fields: str | None) -> bool:
    q = query.casefold()
    return any(f is not None and f.casefold() == q for f in fields)


def _sort_key(query: str, result: GlobalSearchResult) -> tuple[int, int, str]:
    exact_fields: tuple[str | None, ...]
    if result.entity_type == "product":
        exact_fields = (result.title, result.subtitle)
    elif result.entity_type in ("customer", "supplier"):
        exact_fields = (result.title, result.subtitle)
    elif result.entity_type == "sale":
        exact_fields = (result.title,)
    else:
        exact_fields = (result.title, result.subtitle)

    exact_rank = 0 if _is_exact_match(query, *exact_fields) else 1
    return (exact_rank, _ENTITY_ORDER.get(result.entity_type, 99), result.title.casefold())


async def _search_products(
    db: AsyncSession,
    business_id: UUID,
    pattern: str,
) -> list[GlobalSearchResult]:
    sql = """
        SELECT id, name, sku, product_type, is_active
        FROM products
        WHERE business_id = :business_id
            AND deleted_at IS NULL
            AND (name ILIKE :pattern OR sku ILIKE :pattern)
        ORDER BY name
        LIMIT :limit
    """
    result = await db.execute(
        text(sql),
        {"business_id": business_id, "pattern": pattern, "limit": _PER_ENTITY_LIMIT},
    )
    return [
        GlobalSearchResult(
            entity_type="product",
            entity_id=row.id,
            title=row.name,
            subtitle=row.sku,
            meta={
                "product_type": row.product_type,
                "is_active": row.is_active,
            },
        )
        for row in result
    ]


async def _search_customers(
    db: AsyncSession,
    business_id: UUID,
    pattern: str,
) -> list[GlobalSearchResult]:
    sql = """
        SELECT id, name, phone, credit_limit
        FROM customers
        WHERE business_id = :business_id
            AND deleted_at IS NULL
            AND (name ILIKE :pattern OR phone ILIKE :pattern)
        ORDER BY name
        LIMIT :limit
    """
    result = await db.execute(
        text(sql),
        {"business_id": business_id, "pattern": pattern, "limit": _PER_ENTITY_LIMIT},
    )
    return [
        GlobalSearchResult(
            entity_type="customer",
            entity_id=row.id,
            title=row.name,
            subtitle=row.phone,
            meta={"credit_limit": _meta_value(row.credit_limit)},
        )
        for row in result
    ]


async def _search_suppliers(
    db: AsyncSession,
    business_id: UUID,
    pattern: str,
) -> list[GlobalSearchResult]:
    sql = """
        SELECT id, name, phone, payment_terms_days
        FROM suppliers
        WHERE business_id = :business_id
            AND deleted_at IS NULL
            AND (name ILIKE :pattern OR phone ILIKE :pattern)
        ORDER BY name
        LIMIT :limit
    """
    result = await db.execute(
        text(sql),
        {"business_id": business_id, "pattern": pattern, "limit": _PER_ENTITY_LIMIT},
    )
    return [
        GlobalSearchResult(
            entity_type="supplier",
            entity_id=row.id,
            title=row.name,
            subtitle=row.phone,
            meta={"payment_terms_days": row.payment_terms_days},
        )
        for row in result
    ]


async def _search_sales(
    db: AsyncSession,
    business_id: UUID,
    pattern: str,
) -> list[GlobalSearchResult]:
    sql = """
        SELECT id, sale_number, status, sold_at, sale_type
        FROM sales
        WHERE business_id = :business_id
            AND deleted_at IS NULL
            AND sale_number ILIKE :pattern
        ORDER BY sold_at DESC
        LIMIT :limit
    """
    result = await db.execute(
        text(sql),
        {"business_id": business_id, "pattern": pattern, "limit": _PER_ENTITY_LIMIT},
    )
    return [
        GlobalSearchResult(
            entity_type="sale",
            entity_id=row.id,
            title=row.sale_number,
            subtitle=row.status,
            meta={
                "sold_at": _meta_value(row.sold_at),
                "sale_type": row.sale_type,
            },
        )
        for row in result
    ]


async def _search_expenses(
    db: AsyncSession,
    business_id: UUID,
    pattern: str,
) -> list[GlobalSearchResult]:
    sql = """
        SELECT id, expense_number, description, amount, expense_date
        FROM expenses
        WHERE business_id = :business_id
            AND deleted_at IS NULL
            AND (
                expense_number ILIKE :pattern
                OR description ILIKE :pattern
            )
        ORDER BY expense_date DESC
        LIMIT :limit
    """
    result = await db.execute(
        text(sql),
        {"business_id": business_id, "pattern": pattern, "limit": _PER_ENTITY_LIMIT},
    )
    return [
        GlobalSearchResult(
            entity_type="expense",
            entity_id=row.id,
            title=row.expense_number,
            subtitle=row.description,
            meta={
                "amount": _meta_value(row.amount),
                "expense_date": _meta_value(row.expense_date),
            },
        )
        for row in result
    ]


async def global_search(
    db: AsyncSession,
    business_id: UUID,
    query: str,
    limit: int = 20,
) -> GlobalSearchResponse:
    trimmed = query.strip()
    if len(trimmed) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Search query must be at least 2 characters",
        )

    pattern = f"%{trimmed}%"

    products, customers, suppliers, sales, expenses = await asyncio.gather(
        _search_products(db, business_id, pattern),
        _search_customers(db, business_id, pattern),
        _search_suppliers(db, business_id, pattern),
        _search_sales(db, business_id, pattern),
        _search_expenses(db, business_id, pattern),
    )

    combined = products + customers + suppliers + sales + expenses
    combined.sort(key=lambda item: _sort_key(trimmed, item))
    results = combined[:limit]

    return GlobalSearchResponse(
        query=trimmed,
        total=len(results),
        results=results,
    )
