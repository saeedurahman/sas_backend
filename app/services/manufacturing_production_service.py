"""Production order lifecycle and FIFO-backed completion."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ProductionOrderStatusEnum
from app.models.manufacturing import BomHeader, BomLine, ProductionLine, ProductionOrder
from app.schemas.manufacturing import (
    CompleteProductionOrderRequest,
    CreateProductionOrderRequest,
    ProductionLineResponse,
    ProductionOrderBomSummary,
    ProductionOrderResponse,
    UpdateProductionOrderRequest,
)
from app.services.manufacturing_bom_service import (
    _active_bom_lines,
    compute_ingredient_qty_for_production,
)
from app.services.stock_service import (
    generate_document_number,
    get_allow_negative_stock,
    verify_branch,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_production_order_response(order: ProductionOrder) -> ProductionOrderResponse:
    bom = order.bom_header
    active_lines = [line for line in order.lines if line.deleted_at is None]
    return ProductionOrderResponse(
        id=order.id,
        business_id=order.business_id,
        branch_id=order.branch_id,
        bom_header_id=order.bom_header_id,
        production_number=order.production_number,
        status=order.status,
        qty_to_produce=order.qty_to_produce,
        qty_produced=order.qty_produced,
        started_at=order.started_at,
        completed_at=order.completed_at,
        notes=order.notes,
        created_at=order.created_at,
        updated_at=order.updated_at,
        deleted_at=order.deleted_at,
        bom=ProductionOrderBomSummary(
            id=bom.id,
            name=bom.name,
            product_id=bom.product_id,
            product_name=bom.product.name,
            variation_id=bom.variation_id,
            output_qty=bom.output_qty,
            version=bom.version,
        ),
        lines=[
            ProductionLineResponse(
                id=line.id,
                product_id=line.product_id,
                product_name=line.product.name,
                variation_id=line.variation_id,
                qty_consumed=line.qty_consumed,
                cost_per_unit=line.cost_per_unit,
            )
            for line in active_lines
        ],
    )


def _production_order_load_options():
    return (
        selectinload(ProductionOrder.bom_header).selectinload(BomHeader.product),
        selectinload(ProductionOrder.branch),
        selectinload(ProductionOrder.lines).selectinload(ProductionLine.product),
    )


async def _get_production_order(
    db: AsyncSession,
    order_id: UUID,
    business_id: UUID,
) -> ProductionOrder:
    result = await db.execute(
        select(ProductionOrder)
        .where(
            ProductionOrder.id == order_id,
            ProductionOrder.business_id == business_id,
            ProductionOrder.deleted_at.is_(None),
        )
        .options(*_production_order_load_options())
        .execution_options(populate_existing=True)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Production order not found",
        )
    return order


async def _verify_active_bom(
    db: AsyncSession,
    bom_header_id: UUID,
    business_id: UUID,
) -> BomHeader:
    result = await db.execute(
        select(BomHeader)
        .where(
            BomHeader.id == bom_header_id,
            BomHeader.business_id == business_id,
            BomHeader.deleted_at.is_(None),
        )
        .options(
            selectinload(BomHeader.lines),
            selectinload(BomHeader.product),
        )
    )
    bom = result.scalar_one_or_none()
    if bom is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="BOM not found",
        )
    if not bom.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Production order requires an active BOM",
        )
    if not _active_bom_lines(bom):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="BOM has no ingredient lines",
        )
    return bom


def _assert_draft(order: ProductionOrder) -> None:
    if order.status != ProductionOrderStatusEnum.draft.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Production order must be in draft status (current: {order.status})",
        )


def _assert_cancellable(order: ProductionOrder) -> None:
    if order.status in (
        ProductionOrderStatusEnum.completed.value,
        ProductionOrderStatusEnum.cancelled.value,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel production order in status '{order.status}'",
        )


async def get_production_orders(
    db: AsyncSession,
    business_id: UUID,
    *,
    branch_id: UUID | None = None,
    status_filter: str | None = None,
    bom_header_id: UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> list[ProductionOrderResponse]:
    filters = [
        ProductionOrder.business_id == business_id,
        ProductionOrder.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(ProductionOrder.branch_id == branch_id)
    if status_filter is not None:
        filters.append(ProductionOrder.status == status_filter)
    if bom_header_id is not None:
        filters.append(ProductionOrder.bom_header_id == bom_header_id)

    result = await db.execute(
        select(ProductionOrder)
        .where(*filters)
        .options(*_production_order_load_options())
        .order_by(ProductionOrder.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return [
        _build_production_order_response(order)
        for order in result.scalars().unique().all()
    ]


async def get_production_order_by_id(
    db: AsyncSession,
    order_id: UUID,
    business_id: UUID,
) -> ProductionOrderResponse:
    order = await _get_production_order(db, order_id, business_id)
    return _build_production_order_response(order)


async def create_production_order(
    db: AsyncSession,
    business_id: UUID,
    data: CreateProductionOrderRequest,
    created_by: UUID,
) -> ProductionOrderResponse:
    now = _now()

    try:
        await verify_branch(db, data.branch_id, business_id)
        await _verify_active_bom(db, data.bom_header_id, business_id)

        production_number = await generate_document_number(
            db,
            business_id,
            "PRD",
            ProductionOrder,
            ProductionOrder.production_number,
        )

        order = ProductionOrder(
            business_id=business_id,
            branch_id=data.branch_id,
            bom_header_id=data.bom_header_id,
            production_number=production_number,
            status=ProductionOrderStatusEnum.draft.value,
            qty_to_produce=data.qty_to_produce,
            qty_produced=Decimal("0"),
            notes=data.notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(order)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_production_order_by_id(db, order.id, business_id)


async def update_production_order(
    db: AsyncSession,
    order_id: UUID,
    business_id: UUID,
    data: UpdateProductionOrderRequest,
    updated_by: UUID,
) -> ProductionOrderResponse:
    order = await _get_production_order(db, order_id, business_id)
    _assert_draft(order)
    now = _now()

    try:
        if data.bom_header_id is not None:
            await _verify_active_bom(db, data.bom_header_id, business_id)
            order.bom_header_id = data.bom_header_id
        if data.qty_to_produce is not None:
            order.qty_to_produce = data.qty_to_produce
        if data.notes is not None:
            order.notes = data.notes

        order.updated_by = updated_by
        order.updated_at = now
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_production_order_by_id(db, order_id, business_id)


async def start_production_order(
    db: AsyncSession,
    order_id: UUID,
    business_id: UUID,
    started_by: UUID,
) -> ProductionOrderResponse:
    order = await _get_production_order(db, order_id, business_id)
    _assert_draft(order)
    now = _now()

    try:
        await _verify_active_bom(db, order.bom_header_id, business_id)
        order.status = ProductionOrderStatusEnum.in_progress.value
        order.started_at = now
        order.updated_by = started_by
        order.updated_at = now
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_production_order_by_id(db, order_id, business_id)


async def cancel_production_order(
    db: AsyncSession,
    order_id: UUID,
    business_id: UUID,
    cancelled_by: UUID,
) -> ProductionOrderResponse:
    order = await _get_production_order(db, order_id, business_id)
    _assert_cancellable(order)
    now = _now()

    try:
        order.status = ProductionOrderStatusEnum.cancelled.value
        order.updated_by = cancelled_by
        order.updated_at = now
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_production_order_by_id(db, order_id, business_id)


async def _get_bom_lines_for_production(
    db: AsyncSession,
    bom_header_id: UUID,
    business_id: UUID,
) -> list[BomLine]:
    result = await db.execute(
        select(BomLine)
        .where(
            BomLine.bom_header_id == bom_header_id,
            BomLine.business_id == business_id,
            BomLine.deleted_at.is_(None),
        )
        .options(selectinload(BomLine.ingredient_product))
        .order_by(BomLine.sort_order.asc(), BomLine.id.asc())
    )
    lines = list(result.scalars().all())
    if not lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="BOM has no ingredient lines",
        )
    return lines


async def _consume_ingredient_fifo(
    db: AsyncSession,
    *,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    qty: Decimal,
    production_line_id: UUID,
    created_by: UUID,
    movement_at: datetime,
    allow_negative: bool,
) -> tuple[Decimal, Decimal]:
    from app.models.enums import ReferenceTypeEnum, StockMovementTypeEnum
    from app.services.fifo_service import allocate_sale_line_fifo
    from app.services.stock_service import check_sufficient_stock, create_stock_movement

    if not allow_negative:
        sufficient = await check_sufficient_stock(
            db,
            business_id,
            branch_id,
            product_id,
            variation_id,
            qty,
        )
        if not sufficient:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Insufficient ingredient stock for production",
            )

    weighted_cost, consumptions, _short_qty = await allocate_sale_line_fifo(
        db,
        business_id,
        product_id,
        variation_id,
        qty,
    )

    for index, consumption in enumerate(consumptions):
        await create_stock_movement(
            db=db,
            business_id=business_id,
            branch_id=branch_id,
            product_id=product_id,
            variation_id=variation_id,
            movement_type=StockMovementTypeEnum.production_out,
            qty=-consumption.qty,
            cost_per_unit=consumption.cost_per_unit,
            reference_type=ReferenceTypeEnum.production_line,
            reference_id=production_line_id,
            created_by=created_by,
            purchase_line_id=consumption.purchase_line_id,
            movement_at=movement_at,
            movement_sequence=index,
        )

    total_cost = sum(
        (consumption.qty * consumption.cost_per_unit for consumption in consumptions),
        Decimal("0"),
    )
    return weighted_cost, total_cost


async def complete_production_order(
    db: AsyncSession,
    order_id: UUID,
    business_id: UUID,
    data: CompleteProductionOrderRequest,
    completed_by: UUID,
) -> ProductionOrderResponse:
    from app.models.enums import ReferenceTypeEnum, StockMovementTypeEnum
    from app.models.inventory import PurchaseLine
    from app.services.invoice_service import _round2
    from app.services.stock_service import create_stock_movement

    order = await _get_production_order(db, order_id, business_id)
    if order.status != ProductionOrderStatusEnum.in_progress.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Production order must be in progress to complete (current: {order.status})",
        )
    if order.qty_produced > Decimal("0"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Production order has already been completed",
        )
    if data.qty_produced > order.qty_to_produce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="qty_produced cannot exceed qty_to_produce",
        )

    bom = order.bom_header
    bom_lines = await _get_bom_lines_for_production(
        db, order.bom_header_id, business_id
    )
    now = _now()
    allow_negative = await get_allow_negative_stock(db, business_id)

    try:
        total_ingredient_cost = Decimal("0")

        for bom_line in bom_lines:
            _, ingredient_qty = compute_ingredient_qty_for_production(
                qty_required=bom_line.qty_required,
                output_qty=bom.output_qty,
                wastage_pct=bom_line.wastage_pct,
                qty_to_produce=data.qty_produced,
            )
            if ingredient_qty <= Decimal("0"):
                continue

            production_line = ProductionLine(
                business_id=business_id,
                production_order_id=order.id,
                bom_line_id=bom_line.id,
                product_id=bom_line.ingredient_product_id,
                variation_id=bom_line.ingredient_variation_id,
                qty_consumed=ingredient_qty,
                cost_per_unit=Decimal("0"),
                created_by=completed_by,
                created_at=now,
                updated_at=now,
            )
            db.add(production_line)
            await db.flush()

            weighted_cost, line_total_cost = await _consume_ingredient_fifo(
                db,
                business_id=business_id,
                branch_id=order.branch_id,
                product_id=bom_line.ingredient_product_id,
                variation_id=bom_line.ingredient_variation_id,
                qty=ingredient_qty,
                production_line_id=production_line.id,
                created_by=completed_by,
                movement_at=now,
                allow_negative=allow_negative,
            )
            production_line.cost_per_unit = weighted_cost
            total_ingredient_cost += line_total_cost

        finished_unit_cost = _round2(total_ingredient_cost / data.qty_produced)

        finished_layer = PurchaseLine(
            business_id=business_id,
            purchase_order_id=None,
            production_order_id=order.id,
            product_id=bom.product_id,
            variation_id=bom.variation_id,
            ordered_qty=data.qty_produced,
            received_qty=data.qty_produced,
            qty_remaining=data.qty_produced,
            cost_per_unit=finished_unit_cost,
            tax_rate=Decimal("0"),
            created_at=now,
            updated_at=now,
            created_by=completed_by,
        )
        db.add(finished_layer)
        await db.flush()

        await create_stock_movement(
            db=db,
            business_id=business_id,
            branch_id=order.branch_id,
            product_id=bom.product_id,
            variation_id=bom.variation_id,
            movement_type=StockMovementTypeEnum.production_in,
            qty=data.qty_produced,
            cost_per_unit=finished_unit_cost,
            reference_type=ReferenceTypeEnum.production_order,
            reference_id=order.id,
            created_by=completed_by,
            purchase_line_id=finished_layer.id,
            movement_at=now,
        )

        order.qty_produced = data.qty_produced
        order.status = ProductionOrderStatusEnum.completed.value
        order.completed_at = now
        order.updated_by = completed_by
        order.updated_at = now

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_production_order_by_id(db, order_id, business_id)
