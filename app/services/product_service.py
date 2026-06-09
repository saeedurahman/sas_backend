"""Product catalog management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.product import (
    Barcode,
    Brand,
    Category,
    PriceListItem,
    Product,
    ProductLocation,
    ProductVariation,
    Unit,
)
from app.schemas.product import (
    CreateBarcodeRequest,
    CreateProductRequest,
    CreateVariationRequest,
    UpdateProductRequest,
    UpdateVariationRequest,
)


def _product_detail_options():
    return (
        selectinload(Product.category).selectinload(Category.children),
        selectinload(Product.brand),
        selectinload(Product.base_unit),
        selectinload(Product.variations).selectinload(ProductVariation.barcodes),
        selectinload(Product.variations).selectinload(ProductVariation.unit),
        selectinload(Product.barcodes),
    )


async def _check_sku_unique(
    db: AsyncSession,
    business_id: UUID,
    sku: str,
    exclude_product_id: UUID | None = None,
) -> None:
    stmt = select(Product.id).where(
        Product.business_id == business_id,
        Product.sku == sku,
        Product.deleted_at.is_(None),
    )
    if exclude_product_id is not None:
        stmt = stmt.where(Product.id != exclude_product_id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SKU already exists",
        )


async def _check_variation_sku_unique(
    db: AsyncSession,
    business_id: UUID,
    sku: str,
    exclude_variation_id: UUID | None = None,
) -> None:
    stmt = select(ProductVariation.id).where(
        ProductVariation.business_id == business_id,
        ProductVariation.sku == sku,
        ProductVariation.deleted_at.is_(None),
    )
    if exclude_variation_id is not None:
        stmt = stmt.where(ProductVariation.id != exclude_variation_id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Variation SKU already exists",
        )


async def _check_barcode_unique(
    db: AsyncSession,
    business_id: UUID,
    barcode_value: str,
) -> None:
    result = await db.execute(
        select(Barcode.id).where(
            Barcode.business_id == business_id,
            Barcode.barcode == barcode_value,
            Barcode.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Barcode already exists",
        )


async def _verify_category(
    db: AsyncSession,
    category_id: UUID,
    business_id: UUID,
) -> None:
    result = await db.execute(
        select(Category.id).where(
            Category.id == category_id,
            Category.business_id == business_id,
            Category.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category not found",
        )


async def _verify_brand(
    db: AsyncSession,
    brand_id: UUID,
    business_id: UUID,
) -> None:
    result = await db.execute(
        select(Brand.id).where(
            Brand.id == brand_id,
            Brand.business_id == business_id,
            Brand.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Brand not found",
        )


async def _verify_unit(
    db: AsyncSession,
    unit_id: UUID,
    business_id: UUID,
) -> None:
    result = await db.execute(
        select(Unit.id).where(
            Unit.id == unit_id,
            Unit.business_id == business_id,
            Unit.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unit not found",
        )


async def get_products(
    db: AsyncSession,
    business_id: UUID,
    category_id: UUID | None = None,
    brand_id: UUID | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[Product], int]:
    filters = [
        Product.business_id == business_id,
        Product.deleted_at.is_(None),
    ]
    if category_id is not None:
        filters.append(Product.category_id == category_id)
    if brand_id is not None:
        filters.append(Product.brand_id == brand_id)
    if is_active is not None:
        filters.append(Product.is_active.is_(is_active))
    if search:
        pattern = f"%{search}%"
        filters.append(or_(Product.name.ilike(pattern), Product.sku.ilike(pattern)))

    count_result = await db.execute(
        select(func.count()).select_from(Product).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Product)
        .where(*filters)
        .options(
            selectinload(Product.category),
            selectinload(Product.brand),
            selectinload(Product.base_unit),
        )
        .order_by(Product.name)
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def get_product_by_id(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
) -> Product:
    result = await db.execute(
        select(Product)
        .where(
            Product.id == product_id,
            Product.business_id == business_id,
            Product.deleted_at.is_(None),
        )
        .options(*_product_detail_options())
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )
    return product


async def _unset_default_variations(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    updated_by: UUID,
    now: datetime,
) -> None:
    result = await db.execute(
        select(ProductVariation).where(
            ProductVariation.product_id == product_id,
            ProductVariation.business_id == business_id,
            ProductVariation.deleted_at.is_(None),
            ProductVariation.is_default.is_(True),
        )
    )
    for variation in result.scalars().all():
        variation.is_default = False
        variation.updated_by = updated_by
        variation.updated_at = now


async def _create_variations(
    db: AsyncSession,
    product: Product,
    variations_data: list[CreateVariationRequest],
    created_by: UUID,
    now: datetime,
) -> None:
    if not variations_data:
        variation = ProductVariation(
            business_id=product.business_id,
            product_id=product.id,
            name="Default",
            is_default=True,
            is_active=True,
            unit_id=product.base_unit_id,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(variation)
        return

    default_assigned = False
    for index, var_data in enumerate(variations_data):
        if var_data.sku:
            await _check_variation_sku_unique(db, product.business_id, var_data.sku)
        if var_data.unit_id is not None:
            await _verify_unit(db, var_data.unit_id, product.business_id)

        is_default = var_data.is_default
        if len(variations_data) == 1:
            is_default = True
        elif not default_assigned and is_default:
            default_assigned = True
        elif not default_assigned and index == 0 and not any(
            v.is_default for v in variations_data
        ):
            is_default = True
            default_assigned = True
        else:
            is_default = False

        if is_default:
            await _unset_default_variations(
                db, product.id, product.business_id, created_by, now
            )

        variation = ProductVariation(
            business_id=product.business_id,
            product_id=product.id,
            name=var_data.name,
            sku=var_data.sku,
            unit_id=var_data.unit_id,
            is_default=is_default,
            is_active=True,
            weight_grams=var_data.weight_grams,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(variation)


async def create_product(
    db: AsyncSession,
    business_id: UUID,
    data: CreateProductRequest,
    created_by: UUID,
) -> Product:
    now = datetime.now(timezone.utc)

    try:
        if data.category_id is not None:
            await _verify_category(db, data.category_id, business_id)
        if data.brand_id is not None:
            await _verify_brand(db, data.brand_id, business_id)
        if data.base_unit_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="base_unit_id is required",
            )
        unit_result = await db.execute(
            select(Unit.id).where(
                Unit.id == data.base_unit_id,
                Unit.business_id == business_id,
                Unit.deleted_at.is_(None),
            )
        )
        if unit_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unit not found or does not belong to this business",
            )
        if data.sku:
            await _check_sku_unique(db, business_id, data.sku)

        product = Product(
            business_id=business_id,
            category_id=data.category_id,
            brand_id=data.brand_id,
            base_unit_id=data.base_unit_id,
            name=data.name,
            sku=data.sku,
            product_type=data.product_type.value,
            tracking_type=data.tracking_type.value,
            is_sellable=data.is_sellable,
            is_purchasable=data.is_purchasable,
            is_active=True,
            description=data.description,
            image_url=data.image_url,
            shelf_life_days=data.shelf_life_days,
            min_stock_level=data.min_stock_level,
            max_stock_level=data.max_stock_level,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(product)
        await db.flush()

        await _create_variations(db, product, data.variations, created_by, now)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_product_by_id(db, product.id, business_id)


async def update_product(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    data: UpdateProductRequest,
    updated_by: UUID,
) -> Product:
    product = await get_product_by_id(db, product_id, business_id)
    now = datetime.now(timezone.utc)

    if data.category_id is not None:
        await _verify_category(db, data.category_id, business_id)
        product.category_id = data.category_id
    if data.brand_id is not None:
        await _verify_brand(db, data.brand_id, business_id)
        product.brand_id = data.brand_id
    if data.sku is not None and data.sku != product.sku:
        await _check_sku_unique(db, business_id, data.sku, exclude_product_id=product_id)
        product.sku = data.sku
    if data.name is not None:
        product.name = data.name
    if data.is_sellable is not None:
        product.is_sellable = data.is_sellable
    if data.is_purchasable is not None:
        product.is_purchasable = data.is_purchasable
    if data.is_active is not None:
        product.is_active = data.is_active
    if data.description is not None:
        product.description = data.description
    if data.image_url is not None:
        product.image_url = data.image_url
    if data.shelf_life_days is not None:
        product.shelf_life_days = data.shelf_life_days
    if data.min_stock_level is not None:
        product.min_stock_level = data.min_stock_level
    if data.max_stock_level is not None:
        product.max_stock_level = data.max_stock_level

    product.updated_by = updated_by
    product.updated_at = now
    await db.commit()
    return await get_product_by_id(db, product_id, business_id)


async def add_variation(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    data: CreateVariationRequest,
    created_by: UUID,
) -> ProductVariation:
    product = await get_product_by_id(db, product_id, business_id)
    now = datetime.now(timezone.utc)

    if data.sku:
        await _check_variation_sku_unique(db, business_id, data.sku)
    if data.unit_id is not None:
        await _verify_unit(db, data.unit_id, business_id)

    if data.is_default:
        await _unset_default_variations(
            db, product_id, business_id, created_by, now
        )

    variation = ProductVariation(
        business_id=business_id,
        product_id=product_id,
        name=data.name,
        sku=data.sku,
        unit_id=data.unit_id,
        is_default=data.is_default,
        is_active=True,
        weight_grams=data.weight_grams,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(variation)
    await db.commit()
    await db.refresh(variation)

    result = await db.execute(
        select(ProductVariation)
        .where(ProductVariation.id == variation.id)
        .options(
            selectinload(ProductVariation.barcodes),
            selectinload(ProductVariation.unit),
        )
    )
    return result.scalar_one()


async def update_variation(
    db: AsyncSession,
    variation_id: UUID,
    product_id: UUID,
    business_id: UUID,
    data: UpdateVariationRequest,
    updated_by: UUID,
) -> ProductVariation:
    await get_product_by_id(db, product_id, business_id)

    result = await db.execute(
        select(ProductVariation)
        .where(
            ProductVariation.id == variation_id,
            ProductVariation.product_id == product_id,
            ProductVariation.business_id == business_id,
            ProductVariation.deleted_at.is_(None),
        )
        .options(
            selectinload(ProductVariation.barcodes),
            selectinload(ProductVariation.unit),
        )
    )
    variation = result.scalar_one_or_none()
    if variation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Variation not found",
        )

    now = datetime.now(timezone.utc)

    if data.sku is not None and data.sku != variation.sku:
        await _check_variation_sku_unique(
            db, business_id, data.sku, exclude_variation_id=variation_id
        )
        variation.sku = data.sku
    if data.name is not None:
        variation.name = data.name
    if data.unit_id is not None:
        await _verify_unit(db, data.unit_id, business_id)
        variation.unit_id = data.unit_id
    if data.is_active is not None:
        variation.is_active = data.is_active
    if data.weight_grams is not None:
        variation.weight_grams = data.weight_grams

    variation.updated_by = updated_by
    variation.updated_at = now
    await db.commit()
    await db.refresh(variation)
    return variation


async def add_barcode(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    data: CreateBarcodeRequest,
    created_by: UUID,
) -> Barcode:
    product = await get_product_by_id(db, product_id, business_id)
    now = datetime.now(timezone.utc)

    await _check_barcode_unique(db, business_id, data.barcode)

    if data.variation_id is not None:
        var_result = await db.execute(
            select(ProductVariation.id).where(
                ProductVariation.id == data.variation_id,
                ProductVariation.product_id == product_id,
                ProductVariation.business_id == business_id,
                ProductVariation.deleted_at.is_(None),
            )
        )
        if var_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Variation not found for this product",
            )

    if data.is_primary:
        primary_result = await db.execute(
            select(Barcode).where(
                Barcode.product_id == product_id,
                Barcode.business_id == business_id,
                Barcode.deleted_at.is_(None),
                Barcode.is_primary.is_(True),
            )
        )
        for existing in primary_result.scalars().all():
            existing.is_primary = False
            existing.updated_by = created_by
            existing.updated_at = now

    barcode = Barcode(
        business_id=business_id,
        product_id=product.id,
        variation_id=data.variation_id,
        barcode=data.barcode,
        barcode_type=data.barcode_type,
        is_primary=data.is_primary,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(barcode)
    await db.commit()
    await db.refresh(barcode)
    return barcode


async def search_by_barcode(
    db: AsyncSession,
    business_id: UUID,
    barcode_value: str,
) -> Product:
    result = await db.execute(
        select(Barcode).where(
            Barcode.barcode == barcode_value,
            Barcode.business_id == business_id,
            Barcode.deleted_at.is_(None),
        )
    )
    barcode = result.scalar_one_or_none()
    if barcode is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found for barcode",
        )

    return await get_product_by_id(db, barcode.product_id, business_id)


async def delete_product(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    await get_product_by_id(db, product_id, business_id)
    now = datetime.now(timezone.utc)

    try:
        loc_result = await db.execute(
            select(ProductLocation).where(
                ProductLocation.product_id == product_id,
                ProductLocation.business_id == business_id,
                ProductLocation.deleted_at.is_(None),
            )
        )
        for loc in loc_result.scalars().all():
            loc.deleted_at = now
            loc.deleted_by = deleted_by
            loc.updated_at = now
            loc.updated_by = deleted_by

        barcode_result = await db.execute(
            select(Barcode).where(
                Barcode.product_id == product_id,
                Barcode.business_id == business_id,
                Barcode.deleted_at.is_(None),
            )
        )
        for bc in barcode_result.scalars().all():
            bc.deleted_at = now
            bc.deleted_by = deleted_by
            bc.updated_at = now
            bc.updated_by = deleted_by

        price_result = await db.execute(
            select(PriceListItem).where(
                PriceListItem.product_id == product_id,
                PriceListItem.business_id == business_id,
                PriceListItem.deleted_at.is_(None),
            )
        )
        for item in price_result.scalars().all():
            item.deleted_at = now
            item.deleted_by = deleted_by
            item.updated_at = now
            item.updated_by = deleted_by

        var_result = await db.execute(
            select(ProductVariation).where(
                ProductVariation.product_id == product_id,
                ProductVariation.business_id == business_id,
                ProductVariation.deleted_at.is_(None),
            )
        )
        for variation in var_result.scalars().all():
            variation.deleted_at = now
            variation.deleted_by = deleted_by
            variation.updated_at = now
            variation.updated_by = deleted_by

        product_result = await db.execute(
            select(Product).where(
                Product.id == product_id,
                Product.business_id == business_id,
                Product.deleted_at.is_(None),
            )
        )
        product = product_result.scalar_one()
        product.deleted_at = now
        product.deleted_by = deleted_by
        product.updated_at = now
        product.updated_by = deleted_by

        await db.commit()
    except Exception:
        await db.rollback()
        raise
