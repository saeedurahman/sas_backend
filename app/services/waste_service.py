"""Waste entry services."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ReferenceTypeEnum, StockMovementTypeEnum
from app.models.inventory import WasteEntry, WasteEntryLine
from app.schemas.inventory import CreateWasteEntryRequest
from app.services.stock_service import (
    _now,
    check_sufficient_stock,
    create_stock_movement,
    generate_document_number,
    get_allow_negative_stock,
    verify_branch,
    verify_product,
    verify_variation,
)


async def get_waste_entries(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[WasteEntry], int]:
    filters = [
        WasteEntry.business_id == business_id,
        WasteEntry.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(WasteEntry.branch_id == branch_id)

    count_result = await db.execute(
        select(func.count()).select_from(WasteEntry).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(WasteEntry)
        .where(*filters)
        .options(selectinload(WasteEntry.lines))
        .order_by(WasteEntry.wasted_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def get_waste_entry_by_id(
    db: AsyncSession,
    waste_id: UUID,
    business_id: UUID,
) -> WasteEntry:
    result = await db.execute(
        select(WasteEntry)
        .where(
            WasteEntry.id == waste_id,
            WasteEntry.business_id == business_id,
            WasteEntry.deleted_at.is_(None),
        )
        .options(selectinload(WasteEntry.lines))
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Waste entry not found",
        )
    return entry


async def create_waste_entry(
    db: AsyncSession,
    business_id: UUID,
    data: CreateWasteEntryRequest,
    created_by: UUID,
) -> WasteEntry:
    now = _now()
    wasted_at = data.wasted_at or now
    allow_negative = await get_allow_negative_stock(db, business_id)

    try:
        await verify_branch(db, data.branch_id, business_id)

        for line in data.lines:
            product = await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )
            if not allow_negative:
                sufficient = await check_sufficient_stock(
                    db,
                    business_id,
                    data.branch_id,
                    line.product_id,
                    line.variation_id,
                    line.qty,
                )
                if not sufficient:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock for {product.name}",
                    )

        waste_number = await generate_document_number(
            db,
            business_id,
            "WST",
            WasteEntry,
            WasteEntry.waste_number,
        )

        entry = WasteEntry(
            business_id=business_id,
            branch_id=data.branch_id,
            waste_number=waste_number,
            wasted_at=wasted_at,
            reason=data.reason.value,
            notes=data.notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(entry)
        await db.flush()

        for line_data in data.lines:
            waste_line = WasteEntryLine(
                business_id=business_id,
                waste_entry_id=entry.id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty=line_data.qty,
                cost_per_unit=line_data.cost_per_unit,
                batch_number=line_data.batch_number,
                expiry_date=line_data.expiry_date,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(waste_line)
            await db.flush()

            await create_stock_movement(
                db=db,
                business_id=business_id,
                branch_id=data.branch_id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                movement_type=StockMovementTypeEnum.waste,
                qty=-line_data.qty,
                cost_per_unit=line_data.cost_per_unit,
                reference_type=ReferenceTypeEnum.waste_entry_line,
                reference_id=waste_line.id,
                created_by=created_by,
                batch_number=line_data.batch_number,
                expiry_date=line_data.expiry_date,
                movement_at=wasted_at,
            )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_waste_entry_by_id(db, entry.id, business_id)
