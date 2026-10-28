"""Stock transfer services."""

from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ReferenceTypeEnum, StockMovementTypeEnum
from app.models.inventory import StockTransfer, StockTransferLine
from app.schemas.inventory import CreateStockTransferRequest
from app.services.stock_service import (
    _now,
    create_stock_movement,
    generate_document_number,
    get_stock_balance,
    verify_branch,
    verify_product,
    verify_variation,
)


async def get_stock_transfers(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[StockTransfer], int]:
    filters = [
        StockTransfer.business_id == business_id,
        StockTransfer.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(
            (StockTransfer.source_branch_id == branch_id)
            | (StockTransfer.dest_branch_id == branch_id)
        )
    if status is not None:
        filters.append(StockTransfer.status == status)

    count_result = await db.execute(
        select(func.count()).select_from(StockTransfer).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(StockTransfer)
        .where(*filters)
        .options(selectinload(StockTransfer.lines))
        .order_by(StockTransfer.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def get_stock_transfer_by_id(
    db: AsyncSession,
    transfer_id: UUID,
    business_id: UUID,
) -> StockTransfer:
    result = await db.execute(
        select(StockTransfer)
        .where(
            StockTransfer.id == transfer_id,
            StockTransfer.business_id == business_id,
            StockTransfer.deleted_at.is_(None),
        )
        .options(selectinload(StockTransfer.lines))
    )
    transfer = result.scalar_one_or_none()
    if transfer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock transfer not found",
        )
    return transfer


async def create_stock_transfer(
    db: AsyncSession,
    business_id: UUID,
    data: CreateStockTransferRequest,
    created_by: UUID,
) -> StockTransfer:
    now = _now()

    try:
        await verify_branch(db, data.source_branch_id, business_id)
        await verify_branch(db, data.dest_branch_id, business_id)

        for line in data.lines:
            product = await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )
            available = await get_stock_balance(
                db,
                business_id,
                data.source_branch_id,
                line.product_id,
                line.variation_id,
            )
            if available < line.qty:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock at source branch for {product.name}. "
                        f"Available: {available}, Required: {line.qty}"
                    ),
                )

        transfer_number = await generate_document_number(
            db,
            business_id,
            "TRF",
            StockTransfer,
            StockTransfer.transfer_number,
        )

        transfer = StockTransfer(
            business_id=business_id,
            transfer_number=transfer_number,
            source_branch_id=data.source_branch_id,
            dest_branch_id=data.dest_branch_id,
            status="draft",
            notes=data.notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(transfer)
        await db.flush()

        for line_data in data.lines:
            transfer_line = StockTransferLine(
                business_id=business_id,
                stock_transfer_id=transfer.id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty=line_data.qty,
                cost_per_unit=line_data.cost_per_unit,
                qty_received=Decimal("0"),
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(transfer_line)
            await db.flush()

            await create_stock_movement(
                db=db,
                business_id=business_id,
                branch_id=data.source_branch_id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                movement_type=StockMovementTypeEnum.transfer_out,
                qty=-line_data.qty,
                cost_per_unit=line_data.cost_per_unit,
                reference_type=ReferenceTypeEnum.stock_transfer_line,
                reference_id=transfer_line.id,
                created_by=created_by,
                movement_at=now,
            )
            await create_stock_movement(
                db=db,
                business_id=business_id,
                branch_id=data.dest_branch_id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                movement_type=StockMovementTypeEnum.transfer_in,
                qty=line_data.qty,
                cost_per_unit=line_data.cost_per_unit,
                reference_type=ReferenceTypeEnum.stock_transfer_line,
                reference_id=transfer_line.id,
                created_by=created_by,
                movement_at=now,
            )

        transfer.status = "in_transit"
        transfer.transferred_at = now
        transfer.updated_by = created_by
        transfer.updated_at = now

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_stock_transfer_by_id(db, transfer.id, business_id)


async def receive_stock_transfer(
    db: AsyncSession,
    transfer_id: UUID,
    business_id: UUID,
    received_by: UUID,
) -> StockTransfer:
    transfer = await get_stock_transfer_by_id(db, transfer_id, business_id)

    if transfer.status != "in_transit":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Transfer must be in_transit to receive",
        )

    now = _now()
    transfer.status = "received"
    transfer.received_at = now
    transfer.updated_by = received_by
    transfer.updated_at = now

    for line in transfer.lines:
        if line.deleted_at is None:
            line.qty_received = line.qty
            line.updated_at = now

    await db.commit()
    return await get_stock_transfer_by_id(db, transfer_id, business_id)
