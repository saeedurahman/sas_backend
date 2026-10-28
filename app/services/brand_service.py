"""Brand management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Brand, Product
from app.schemas.product import CreateBrandRequest, UpdateBrandRequest


async def get_brands(
    db: AsyncSession,
    business_id: UUID,
) -> list[Brand]:
    result = await db.execute(
        select(Brand)
        .where(
            Brand.business_id == business_id,
            Brand.deleted_at.is_(None),
        )
        .order_by(Brand.name)
    )
    return list(result.scalars().all())


async def get_brand_by_id(
    db: AsyncSession,
    brand_id: UUID,
    business_id: UUID,
) -> Brand:
    result = await db.execute(
        select(Brand).where(
            Brand.id == brand_id,
            Brand.business_id == business_id,
            Brand.deleted_at.is_(None),
        )
    )
    brand = result.scalar_one_or_none()
    if brand is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Brand not found",
        )
    return brand


async def create_brand(
    db: AsyncSession,
    business_id: UUID,
    data: CreateBrandRequest,
    created_by: UUID,
) -> Brand:
    now = datetime.now(timezone.utc)
    brand = Brand(
        business_id=business_id,
        name=data.name,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(brand)
    await db.commit()
    await db.refresh(brand)
    return brand


async def update_brand(
    db: AsyncSession,
    brand_id: UUID,
    business_id: UUID,
    data: UpdateBrandRequest,
    updated_by: UUID,
) -> Brand:
    brand = await get_brand_by_id(db, brand_id, business_id)
    now = datetime.now(timezone.utc)

    if data.name is not None:
        brand.name = data.name
    if data.is_active is not None:
        brand.is_active = data.is_active

    brand.updated_by = updated_by
    brand.updated_at = now
    await db.commit()
    await db.refresh(brand)
    return brand


async def delete_brand(
    db: AsyncSession,
    brand_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    brand = await get_brand_by_id(db, brand_id, business_id)

    count_result = await db.execute(
        select(func.count())
        .select_from(Product)
        .where(
            Product.brand_id == brand_id,
            Product.business_id == business_id,
            Product.deleted_at.is_(None),
            Product.is_active.is_(True),
        )
    )
    if count_result.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete brand with active products",
        )

    now = datetime.now(timezone.utc)
    brand.deleted_at = now
    brand.deleted_by = deleted_by
    brand.updated_at = now
    brand.updated_by = deleted_by
    await db.commit()
