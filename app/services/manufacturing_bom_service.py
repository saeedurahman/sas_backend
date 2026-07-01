"""Bill of Materials (BOM) management."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ProductTypeEnum
from app.models.manufacturing import BomHeader, BomLine
from app.models.product import Product, Unit
from app.schemas.manufacturing import (
    BomHeaderResponse,
    BomLineRequest,
    BomLineResponse,
    BomPreviewLineResponse,
    BomPreviewResponse,
    CreateBomRequest,
    UpdateBomRequest,
)
from app.services.invoice_service import _round2
from app.services.stock_service import verify_product, verify_variation


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active_bom_lines(bom: BomHeader) -> list[BomLine]:
    return [line for line in bom.lines if line.deleted_at is None]


def compute_ingredient_qty_for_production(
    *,
    qty_required: Decimal,
    output_qty: Decimal,
    wastage_pct: Decimal,
    qty_to_produce: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return (qty_per_output_unit with wastage, total qty for batch)."""
    if output_qty <= Decimal("0"):
        raise ValueError("output_qty must be positive")
    per_unit = _round2(
        (qty_required / output_qty) * (Decimal("1") + wastage_pct / Decimal("100"))
    )
    total = _round2(per_unit * qty_to_produce)
    return per_unit, total


def _build_bom_line_response(line: BomLine) -> BomLineResponse:
    return BomLineResponse(
        id=line.id,
        business_id=line.business_id,
        bom_header_id=line.bom_header_id,
        ingredient_product_id=line.ingredient_product_id,
        ingredient_product_name=line.ingredient_product.name,
        ingredient_variation_id=line.ingredient_variation_id,
        qty_required=line.qty_required,
        unit_id=line.unit_id,
        wastage_pct=line.wastage_pct,
        sort_order=line.sort_order,
    )


def _build_bom_response(bom: BomHeader) -> BomHeaderResponse:
    return BomHeaderResponse(
        id=bom.id,
        business_id=bom.business_id,
        product_id=bom.product_id,
        product_name=bom.product.name,
        variation_id=bom.variation_id,
        name=bom.name,
        output_qty=bom.output_qty,
        is_active=bom.is_active,
        version=bom.version,
        created_at=bom.created_at,
        updated_at=bom.updated_at,
        deleted_at=bom.deleted_at,
        lines=[_build_bom_line_response(line) for line in _active_bom_lines(bom)],
    )


async def _verify_manufactured_product(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    *,
    variation_id: UUID | None = None,
) -> Product:
    product = await verify_product(db, product_id, business_id)
    if product.product_type != ProductTypeEnum.manufactured.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="BOM finished product must have product_type 'manufactured'",
        )
    if variation_id is not None:
        await verify_variation(db, variation_id, product_id, business_id)
    return product


async def _verify_unit(
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unit not found",
        )
    return unit


async def _validate_bom_lines(
    db: AsyncSession,
    business_id: UUID,
    finished_product_id: UUID,
    lines: list[BomLineRequest],
) -> None:
    if not lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="BOM must have at least one ingredient line",
        )

    seen: set[tuple[UUID, UUID | None]] = set()
    for line in lines:
        if line.ingredient_product_id == finished_product_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="BOM ingredient cannot be the same as the finished product",
            )

        key = (line.ingredient_product_id, line.ingredient_variation_id)
        if key in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate ingredient line in BOM",
            )
        seen.add(key)

        await verify_product(db, line.ingredient_product_id, business_id)
        if line.ingredient_variation_id is not None:
            await verify_variation(
                db,
                line.ingredient_variation_id,
                line.ingredient_product_id,
                business_id,
            )
        if line.unit_id is not None:
            await _verify_unit(db, line.unit_id, business_id)


def _bom_product_filters(
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
) -> list:
    filters = [
        BomHeader.business_id == business_id,
        BomHeader.product_id == product_id,
        BomHeader.deleted_at.is_(None),
    ]
    if variation_id is None:
        filters.append(BomHeader.variation_id.is_(None))
    else:
        filters.append(BomHeader.variation_id == variation_id)
    return filters


async def _next_bom_version(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
) -> int:
    result = await db.execute(
        select(func.max(BomHeader.version)).where(
            *_bom_product_filters(business_id, product_id, variation_id)
        )
    )
    current_max = result.scalar_one_or_none()
    return int(current_max or 0) + 1


async def _deactivate_other_active_boms(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    *,
    exclude_bom_id: UUID | None,
    updated_by: UUID,
) -> None:
    stmt = select(BomHeader).where(
        *_bom_product_filters(business_id, product_id, variation_id),
        BomHeader.is_active.is_(True),
    )
    if exclude_bom_id is not None:
        stmt = stmt.where(BomHeader.id != exclude_bom_id)

    result = await db.execute(stmt)
    now = _now()
    for bom in result.scalars().all():
        bom.is_active = False
        bom.updated_by = updated_by
        bom.updated_at = now


async def _replace_bom_lines(
    db: AsyncSession,
    bom: BomHeader,
    lines: list[BomLineRequest],
    *,
    created_by: UUID,
) -> None:
    now = _now()
    result = await db.execute(
        select(BomLine).where(
            BomLine.bom_header_id == bom.id,
            BomLine.deleted_at.is_(None),
        )
    )
    for existing in result.scalars().all():
        existing.deleted_at = now
        existing.updated_by = created_by
        existing.updated_at = now

    for line_data in lines:
        db.add(
            BomLine(
                business_id=bom.business_id,
                bom_header_id=bom.id,
                ingredient_product_id=line_data.ingredient_product_id,
                ingredient_variation_id=line_data.ingredient_variation_id,
                qty_required=line_data.qty_required,
                unit_id=line_data.unit_id,
                wastage_pct=line_data.wastage_pct,
                sort_order=line_data.sort_order,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
        )


async def _get_bom_header(
    db: AsyncSession,
    bom_id: UUID,
    business_id: UUID,
) -> BomHeader:
    result = await db.execute(
        select(BomHeader)
        .where(
            BomHeader.id == bom_id,
            BomHeader.business_id == business_id,
            BomHeader.deleted_at.is_(None),
        )
        .options(
            selectinload(BomHeader.lines).selectinload(BomLine.ingredient_product),
            selectinload(BomHeader.lines).selectinload(BomLine.ingredient_variation),
            selectinload(BomHeader.product),
        )
        .execution_options(populate_existing=True)
    )
    bom = result.scalar_one_or_none()
    if bom is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="BOM not found",
        )
    return bom


async def get_bom_headers(
    db: AsyncSession,
    business_id: UUID,
    *,
    product_id: UUID | None = None,
    active_only: bool = False,
) -> list[BomHeaderResponse]:
    stmt = (
        select(BomHeader)
        .where(
            BomHeader.business_id == business_id,
            BomHeader.deleted_at.is_(None),
        )
        .options(
            selectinload(BomHeader.lines).selectinload(BomLine.ingredient_product),
            selectinload(BomHeader.product),
        )
        .order_by(BomHeader.product_id, BomHeader.version.desc())
    )
    if product_id is not None:
        stmt = stmt.where(BomHeader.product_id == product_id)
    if active_only:
        stmt = stmt.where(BomHeader.is_active.is_(True))

    result = await db.execute(stmt)
    return [_build_bom_response(bom) for bom in result.scalars().unique().all()]


async def get_bom_header_by_id(
    db: AsyncSession,
    bom_id: UUID,
    business_id: UUID,
) -> BomHeaderResponse:
    bom = await _get_bom_header(db, bom_id, business_id)
    return _build_bom_response(bom)


async def get_boms_by_product(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    *,
    variation_id: UUID | None = None,
    active_only: bool = False,
) -> list[BomHeaderResponse]:
    await verify_product(db, product_id, business_id)
    stmt = (
        select(BomHeader)
        .where(
            BomHeader.business_id == business_id,
            BomHeader.product_id == product_id,
            BomHeader.deleted_at.is_(None),
        )
        .options(
            selectinload(BomHeader.lines).selectinload(BomLine.ingredient_product),
            selectinload(BomHeader.product),
        )
        .order_by(BomHeader.version.desc())
    )
    if variation_id is not None:
        stmt = stmt.where(BomHeader.variation_id == variation_id)
    if active_only:
        stmt = stmt.where(BomHeader.is_active.is_(True))

    result = await db.execute(stmt)
    return [_build_bom_response(bom) for bom in result.scalars().unique().all()]


async def create_bom_header(
    db: AsyncSession,
    business_id: UUID,
    data: CreateBomRequest,
    created_by: UUID,
) -> BomHeaderResponse:
    await _verify_manufactured_product(
        db, data.product_id, business_id, variation_id=data.variation_id
    )
    await _validate_bom_lines(db, business_id, data.product_id, data.lines)

    now = _now()
    version = await _next_bom_version(
        db, business_id, data.product_id, data.variation_id
    )

    try:
        if data.is_active:
            await _deactivate_other_active_boms(
                db,
                business_id,
                data.product_id,
                data.variation_id,
                exclude_bom_id=None,
                updated_by=created_by,
            )

        bom = BomHeader(
            business_id=business_id,
            product_id=data.product_id,
            variation_id=data.variation_id,
            name=data.name,
            output_qty=data.output_qty,
            is_active=data.is_active,
            version=version,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(bom)
        await db.flush()
        await _replace_bom_lines(db, bom, data.lines, created_by=created_by)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_bom_header_by_id(db, bom.id, business_id)


async def update_bom_header(
    db: AsyncSession,
    bom_id: UUID,
    business_id: UUID,
    data: UpdateBomRequest,
    updated_by: UUID,
) -> BomHeaderResponse:
    bom = await _get_bom_header(db, bom_id, business_id)
    now = _now()

    try:
        if data.name is not None:
            bom.name = data.name
        if data.output_qty is not None:
            bom.output_qty = data.output_qty
        if data.is_active is not None:
            bom.is_active = data.is_active
            if data.is_active:
                await _deactivate_other_active_boms(
                    db,
                    business_id,
                    bom.product_id,
                    bom.variation_id,
                    exclude_bom_id=bom.id,
                    updated_by=updated_by,
                )

        if data.lines is not None:
            await _validate_bom_lines(
                db, business_id, bom.product_id, data.lines
            )
            await _replace_bom_lines(db, bom, data.lines, created_by=updated_by)

        bom.updated_by = updated_by
        bom.updated_at = now
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_bom_header_by_id(db, bom_id, business_id)


async def delete_bom_header(
    db: AsyncSession,
    bom_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    bom = await _get_bom_header(db, bom_id, business_id)
    now = _now()

    try:
        bom.deleted_at = now
        bom.deleted_by = deleted_by
        bom.updated_by = deleted_by
        bom.updated_at = now
        bom.is_active = False

        result = await db.execute(
            select(BomLine).where(
                BomLine.bom_header_id == bom.id,
                BomLine.deleted_at.is_(None),
            )
        )
        for line in result.scalars().all():
            line.deleted_at = now
            line.updated_by = deleted_by
            line.updated_at = now

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise


async def preview_bom_requirements(
    db: AsyncSession,
    business_id: UUID,
    bom_header_id: UUID,
    qty_to_produce: Decimal,
) -> BomPreviewResponse:
    bom = await _get_bom_header(db, bom_header_id, business_id)
    preview_lines: list[BomPreviewLineResponse] = []

    for line in _active_bom_lines(bom):
        per_unit, total = compute_ingredient_qty_for_production(
            qty_required=line.qty_required,
            output_qty=bom.output_qty,
            wastage_pct=line.wastage_pct,
            qty_to_produce=qty_to_produce,
        )
        preview_lines.append(
            BomPreviewLineResponse(
                ingredient_product_id=line.ingredient_product_id,
                ingredient_product_name=line.ingredient_product.name,
                ingredient_variation_id=line.ingredient_variation_id,
                qty_per_output_unit=per_unit,
                total_qty_required=total,
                wastage_pct=line.wastage_pct,
            )
        )

    return BomPreviewResponse(
        bom_header_id=bom.id,
        product_id=bom.product_id,
        output_qty=bom.output_qty,
        qty_to_produce=qty_to_produce,
        lines=preview_lines,
    )
