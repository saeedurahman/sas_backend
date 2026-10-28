"""Price list management services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.product import PriceList, PriceListItem, Product
from app.schemas.product import CreatePriceListRequest, SetPriceRequest


async def get_price_lists(
    db: AsyncSession,
    business_id: UUID,
) -> list[PriceList]:
    result = await db.execute(
        select(PriceList)
        .where(
            PriceList.business_id == business_id,
            PriceList.deleted_at.is_(None),
        )
        .options(selectinload(PriceList.items))
        .order_by(PriceList.name)
    )
    return list(result.scalars().unique().all())


async def create_price_list(
    db: AsyncSession,
    business_id: UUID,
    data: CreatePriceListRequest,
    created_by: UUID,
) -> PriceList:
    now = datetime.now(timezone.utc)

    try:
        if data.is_default:
            existing = await db.execute(
                select(PriceList).where(
                    PriceList.business_id == business_id,
                    PriceList.deleted_at.is_(None),
                    PriceList.is_default.is_(True),
                )
            )
            for price_list in existing.scalars().all():
                price_list.is_default = False
                price_list.updated_by = created_by
                price_list.updated_at = now

        price_list = PriceList(
            business_id=business_id,
            name=data.name,
            list_type=data.list_type.value,
            is_default=data.is_default,
            is_active=True,
            valid_from=data.valid_from,
            valid_to=data.valid_to,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(price_list)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(price_list)
    return await _get_price_list_by_id(db, price_list.id, business_id)


async def _get_price_list_by_id(
    db: AsyncSession,
    price_list_id: UUID,
    business_id: UUID,
) -> PriceList:
    result = await db.execute(
        select(PriceList)
        .where(
            PriceList.id == price_list_id,
            PriceList.business_id == business_id,
            PriceList.deleted_at.is_(None),
        )
        .options(selectinload(PriceList.items))
    )
    price_list = result.scalar_one_or_none()
    if price_list is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Price list not found",
        )
    return price_list


async def _verify_product(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
) -> None:
    result = await db.execute(
        select(Product.id).where(
            Product.id == product_id,
            Product.business_id == business_id,
            Product.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product not found",
        )


async def set_price(
    db: AsyncSession,
    price_list_id: UUID,
    business_id: UUID,
    data: SetPriceRequest,
    created_by: UUID,
) -> PriceListItem:
    await _get_price_list_by_id(db, price_list_id, business_id)
    await _verify_product(db, data.product_id, business_id)

    if data.unit_price < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Price cannot be negative",
        )

    now = datetime.now(timezone.utc)
    stmt = select(PriceListItem).where(
        PriceListItem.price_list_id == price_list_id,
        PriceListItem.product_id == data.product_id,
        PriceListItem.business_id == business_id,
        PriceListItem.deleted_at.is_(None),
    )
    if data.variation_id is None:
        stmt = stmt.where(PriceListItem.variation_id.is_(None))
    else:
        stmt = stmt.where(PriceListItem.variation_id == data.variation_id)

    result = await db.execute(stmt)
    item = result.scalar_one_or_none()

    if item is not None:
        item.unit_price = data.unit_price
        item.min_qty = data.min_qty
        item.updated_by = created_by
        item.updated_at = now
    else:
        item = PriceListItem(
            business_id=business_id,
            price_list_id=price_list_id,
            product_id=data.product_id,
            variation_id=data.variation_id,
            unit_price=data.unit_price,
            min_qty=data.min_qty,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(item)

    await db.commit()
    await db.refresh(item)
    return item


async def get_product_price(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None = None,
    price_list_id: UUID | None = None,
) -> Decimal | None:
    if price_list_id is not None:
        await _get_price_list_by_id(db, price_list_id, business_id)
        target_list_id = price_list_id
    else:
        default_result = await db.execute(
            select(PriceList.id).where(
                PriceList.business_id == business_id,
                PriceList.deleted_at.is_(None),
                PriceList.is_default.is_(True),
            )
        )
        target_list_id = default_result.scalar_one_or_none()
        if target_list_id is None:
            return None

    stmt = select(PriceListItem.unit_price).where(
        PriceListItem.price_list_id == target_list_id,
        PriceListItem.product_id == product_id,
        PriceListItem.business_id == business_id,
        PriceListItem.deleted_at.is_(None),
    )
    if variation_id is None:
        stmt = stmt.where(PriceListItem.variation_id.is_(None))
    else:
        stmt = stmt.where(PriceListItem.variation_id == variation_id)

    result = await db.execute(stmt)
    return result.scalar_one_or_none()
