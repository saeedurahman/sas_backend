"""Discount scheme management services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sales import DiscountScheme
from app.schemas.sales import CreateDiscountSchemeRequest


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_discount_schemes(
    db: AsyncSession,
    business_id: UUID,
) -> list[DiscountScheme]:
    result = await db.execute(
        select(DiscountScheme)
        .where(
            DiscountScheme.business_id == business_id,
            DiscountScheme.deleted_at.is_(None),
        )
        .order_by(DiscountScheme.name)
    )
    return list(result.scalars().all())


async def get_discount_scheme_by_id(
    db: AsyncSession,
    scheme_id: UUID,
    business_id: UUID,
) -> DiscountScheme:
    result = await db.execute(
        select(DiscountScheme).where(
            DiscountScheme.id == scheme_id,
            DiscountScheme.business_id == business_id,
            DiscountScheme.deleted_at.is_(None),
        )
    )
    scheme = result.scalar_one_or_none()
    if scheme is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Discount scheme not found",
        )
    return scheme


async def create_discount_scheme(
    db: AsyncSession,
    business_id: UUID,
    data: CreateDiscountSchemeRequest,
    created_by: UUID,
) -> DiscountScheme:
    now = _now()
    scheme = DiscountScheme(
        business_id=business_id,
        name=data.name,
        discount_type=data.discount_type.value,
        discount_value=data.discount_value,
        min_purchase_amount=data.min_purchase_amount,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        applies_to_json=data.applies_to_json,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(scheme)
    await db.commit()
    await db.refresh(scheme)
    return scheme


async def update_discount_scheme(
    db: AsyncSession,
    scheme_id: UUID,
    business_id: UUID,
    updated_by: UUID,
    name: str | None = None,
    discount_value: Decimal | None = None,
    min_purchase_amount: Decimal | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    is_active: bool | None = None,
    applies_to_json: dict | None = None,
) -> DiscountScheme:
    scheme = await get_discount_scheme_by_id(db, scheme_id, business_id)
    now = _now()

    if name is not None:
        scheme.name = name
    if discount_value is not None:
        scheme.discount_value = discount_value
    if min_purchase_amount is not None:
        scheme.min_purchase_amount = min_purchase_amount
    if valid_from is not None:
        scheme.valid_from = valid_from
    if valid_to is not None:
        scheme.valid_to = valid_to
    if is_active is not None:
        scheme.is_active = is_active
    if applies_to_json is not None:
        scheme.applies_to_json = applies_to_json

    scheme.updated_by = updated_by
    scheme.updated_at = now
    await db.commit()
    await db.refresh(scheme)
    return scheme
