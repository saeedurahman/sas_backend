"""Stock adjustment services."""

from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ReferenceTypeEnum, StockMovementTypeEnum
from app.models.inventory import StockAdjustment, StockAdjustmentLine
from app.schemas.inventory import CreateStockAdjustmentRequest
from app.models.enums import AdjustmentReasonEnum
from app.services.fifo_service import (
    consume_to_movements,
    create_fifo_layer,
    get_fallback_cost_per_unit,
)
from app.services.stock_service import (
    _now,
    check_sufficient_stock,
    create_stock_movement,
    generate_document_number,
    get_allow_negative_stock,
    lock_stock_positions,
    verify_branch,
    verify_product,
    verify_variation,
)


async def get_stock_adjustments(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[StockAdjustment], int]:
    filters = [
        StockAdjustment.business_id == business_id,
        StockAdjustment.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(StockAdjustment.branch_id == branch_id)

    count_result = await db.execute(
        select(func.count()).select_from(StockAdjustment).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(StockAdjustment)
        .where(*filters)
        .options(selectinload(StockAdjustment.lines))
        .order_by(StockAdjustment.adjusted_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def get_stock_adjustment_by_id(
    db: AsyncSession,
    adj_id: UUID,
    business_id: UUID,
) -> StockAdjustment:
    result = await db.execute(
        select(StockAdjustment)
        .where(
            StockAdjustment.id == adj_id,
            StockAdjustment.business_id == business_id,
            StockAdjustment.deleted_at.is_(None),
        )
        .options(selectinload(StockAdjustment.lines))
    )
    adjustment = result.scalar_one_or_none()
    if adjustment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock adjustment not found",
        )
    return adjustment


async def create_stock_adjustment(
    db: AsyncSession,
    business_id: UUID,
    data: CreateStockAdjustmentRequest,
    created_by: UUID,
) -> StockAdjustment:
    """Stock in creates a cost layer, stock out consumes layers (FIFO).

    Costs are server-derived. The one exception is an ``opening_balance``
    adjustment, where there is no history to derive a cost from, so the
    client-supplied ``cost_per_unit`` is honoured; it is ignored for every other
    reason and for stock-out lines.
    """
    now = _now()
    adjusted_at = data.adjusted_at or now
    allow_negative = await get_allow_negative_stock(db, business_id)

    try:
        await verify_branch(db, data.branch_id, business_id)

        for line in data.lines:
            await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )

        await lock_stock_positions(
            db,
            business_id,
            data.branch_id,
            [(l.product_id, l.variation_id) for l in data.lines],
        )

        if not allow_negative:
            out_demand: dict[tuple[UUID, UUID | None], Decimal] = {}
            for line in data.lines:
                if line.qty_delta < 0:
                    key = (line.product_id, line.variation_id)
                    out_demand[key] = out_demand.get(key, Decimal("0")) + abs(
                        line.qty_delta
                    )
            for (product_id, variation_id), qty in out_demand.items():
                if not await check_sufficient_stock(
                    db, business_id, data.branch_id, product_id, variation_id, qty
                ):
                    product = await verify_product(db, product_id, business_id)
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock for {product.name}",
                    )

        adjustment_number = await generate_document_number(
            db,
            business_id,
            "ADJ",
            StockAdjustment,
            StockAdjustment.adjustment_number,
        )

        adjustment = StockAdjustment(
            business_id=business_id,
            branch_id=data.branch_id,
            adjustment_number=adjustment_number,
            reason=data.reason.value,
            adjusted_at=adjusted_at,
            notes=data.notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(adjustment)
        await db.flush()

        for line_data in data.lines:
            adj_line = StockAdjustmentLine(
                business_id=business_id,
                stock_adjustment_id=adjustment.id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty_delta=line_data.qty_delta,
                cost_per_unit=Decimal("0"),
                notes=line_data.notes,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(adj_line)
            await db.flush()

            if line_data.qty_delta > 0:
                if (
                    data.reason == AdjustmentReasonEnum.opening_balance
                    and line_data.cost_per_unit is not None
                ):
                    unit_cost = line_data.cost_per_unit
                else:
                    unit_cost = await get_fallback_cost_per_unit(
                        db, business_id, line_data.product_id, line_data.variation_id
                    )
                layer = await create_fifo_layer(
                    db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=line_data.product_id,
                    variation_id=line_data.variation_id,
                    qty=line_data.qty_delta,
                    cost_per_unit=unit_cost,
                    created_by=created_by,
                    source="adjustment",
                    created_at=adjusted_at,
                )
                adj_line.cost_per_unit = layer.cost_per_unit
                await create_stock_movement(
                    db=db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=line_data.product_id,
                    variation_id=line_data.variation_id,
                    movement_type=StockMovementTypeEnum.adjustment_in,
                    qty=line_data.qty_delta,
                    cost_per_unit=layer.cost_per_unit,
                    reference_type=ReferenceTypeEnum.stock_adjustment_line,
                    reference_id=adj_line.id,
                    created_by=created_by,
                    purchase_line_id=layer.id,
                    notes=line_data.notes,
                    movement_at=adjusted_at,
                )
            else:
                weighted, _slices, _short = await consume_to_movements(
                    db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=line_data.product_id,
                    variation_id=line_data.variation_id,
                    qty=abs(line_data.qty_delta),
                    movement_type=StockMovementTypeEnum.adjustment_out,
                    reference_type=ReferenceTypeEnum.stock_adjustment_line,
                    reference_id=adj_line.id,
                    created_by=created_by,
                    movement_at=adjusted_at,
                    notes=line_data.notes,
                )
                adj_line.cost_per_unit = weighted

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_stock_adjustment_by_id(db, adjustment.id, business_id)
