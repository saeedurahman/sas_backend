"""Unit and unit conversion management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product, ProductVariation, Unit, UnitConversion
from app.schemas.product import (
    CreateUnitConversionRequest,
    CreateUnitRequest,
    UpdateUnitRequest,
)


async def get_units(
    db: AsyncSession,
    business_id: UUID,
) -> list[Unit]:
    result = await db.execute(
        select(Unit)
        .where(
            Unit.business_id == business_id,
            Unit.deleted_at.is_(None),
        )
        .order_by(Unit.name)
    )
    return list(result.scalars().all())


async def get_unit_by_id(
    db: AsyncSession,
    unit_id: UUID,
    business_id: UUID,
) -> Unit:
    result = await db.execute(
        select(Unit).where(
            Unit.id == unit_id,
            Unit.business_id == business_id,
            Unit.deleted_at.is_(None),
        )
    )
    unit = result.scalar_one_or_none()
    if unit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unit not found",
        )
    return unit


async def create_unit(
    db: AsyncSession,
    business_id: UUID,
    data: CreateUnitRequest,
    created_by: UUID,
) -> Unit:
    now = datetime.now(timezone.utc)
    unit = Unit(
        business_id=business_id,
        name=data.name,
        symbol=data.symbol,
        is_base_unit=data.is_base_unit,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(unit)
    await db.commit()
    await db.refresh(unit)
    return unit


async def update_unit(
    db: AsyncSession,
    unit_id: UUID,
    business_id: UUID,
    data: UpdateUnitRequest,
    updated_by: UUID,
) -> Unit:
    unit = await get_unit_by_id(db, unit_id, business_id)
    now = datetime.now(timezone.utc)

    if data.name is not None:
        unit.name = data.name
    if data.symbol is not None:
        unit.symbol = data.symbol
    if data.is_active is not None and not data.is_active:
        unit.deleted_at = now
        unit.deleted_by = updated_by

    unit.updated_by = updated_by
    unit.updated_at = now
    await db.commit()
    await db.refresh(unit)
    return unit


async def delete_unit(
    db: AsyncSession,
    unit_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    unit = await get_unit_by_id(db, unit_id, business_id)

    product_count = await db.execute(
        select(func.count())
        .select_from(Product)
        .where(
            Product.base_unit_id == unit_id,
            Product.business_id == business_id,
            Product.deleted_at.is_(None),
        )
    )
    if product_count.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete unit in use by products",
        )

    variation_count = await db.execute(
        select(func.count())
        .select_from(ProductVariation)
        .where(
            ProductVariation.unit_id == unit_id,
            ProductVariation.business_id == business_id,
            ProductVariation.deleted_at.is_(None),
        )
    )
    if variation_count.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete unit in use by variations",
        )

    now = datetime.now(timezone.utc)
    unit.deleted_at = now
    unit.deleted_by = deleted_by
    unit.updated_at = now
    unit.updated_by = deleted_by
    await db.commit()


async def create_unit_conversion(
    db: AsyncSession,
    business_id: UUID,
    data: CreateUnitConversionRequest,
    created_by: UUID,
) -> UnitConversion:
    if data.from_unit_id == data.to_unit_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_unit and to_unit must be different",
        )

    await get_unit_by_id(db, data.from_unit_id, business_id)
    await get_unit_by_id(db, data.to_unit_id, business_id)

    dup_result = await db.execute(
        select(UnitConversion.id).where(
            UnitConversion.business_id == business_id,
            UnitConversion.from_unit_id == data.from_unit_id,
            UnitConversion.to_unit_id == data.to_unit_id,
            UnitConversion.deleted_at.is_(None),
        )
    )
    if dup_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unit conversion already exists",
        )

    now = datetime.now(timezone.utc)
    conversion = UnitConversion(
        business_id=business_id,
        from_unit_id=data.from_unit_id,
        to_unit_id=data.to_unit_id,
        conversion_factor=data.conversion_factor,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(conversion)
    await db.commit()
    await db.refresh(conversion)
    return conversion


async def get_unit_conversions(
    db: AsyncSession,
    business_id: UUID,
) -> list[UnitConversion]:
    result = await db.execute(
        select(UnitConversion)
        .where(
            UnitConversion.business_id == business_id,
            UnitConversion.deleted_at.is_(None),
        )
        .order_by(UnitConversion.created_at)
    )
    return list(result.scalars().all())
