"""Stock transfer services."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ReferenceTypeEnum, StockMovementTypeEnum
from app.models.inventory import StockMovement, StockTransfer, StockTransferLine
from app.models.user import User
from app.schemas.inventory import CreateStockTransferRequest
from app.services.fifo_service import consume_to_movements, create_fifo_layer
from app.services.stock_service import (
    _now,
    create_stock_movement,
    generate_document_number,
    get_stock_balance,
    lock_stock_positions,
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
    """Dispatch: stock leaves the source now (FIFO, server-derived cost).

    The destination is NOT credited here. The goods are in transit until
    ``receive_stock_transfer`` opens layers at the destination at the same cost.
    """
    now = _now()

    try:
        await verify_branch(db, data.source_branch_id, business_id)
        await verify_branch(db, data.dest_branch_id, business_id)

        for line in data.lines:
            await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )

        await lock_stock_positions(
            db,
            business_id,
            data.source_branch_id,
            [(l.product_id, l.variation_id) for l in data.lines],
        )

        demand: dict[tuple[UUID, UUID | None], Decimal] = {}
        for line in data.lines:
            key = (line.product_id, line.variation_id)
            demand[key] = demand.get(key, Decimal("0")) + line.qty
        for (product_id, variation_id), qty in demand.items():
            available = await get_stock_balance(
                db, business_id, data.source_branch_id, product_id, variation_id
            )
            if available < qty:
                product = await verify_product(db, product_id, business_id)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock at source branch for {product.name}. "
                        f"Available: {available}, Required: {qty}"
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
                cost_per_unit=Decimal("0"),
                qty_received=Decimal("0"),
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(transfer_line)
            await db.flush()

            weighted, _slices, _short = await consume_to_movements(
                db,
                business_id=business_id,
                branch_id=data.source_branch_id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty=line_data.qty,
                movement_type=StockMovementTypeEnum.transfer_out,
                reference_type=ReferenceTypeEnum.stock_transfer_line,
                reference_id=transfer_line.id,
                created_by=created_by,
                movement_at=now,
            )
            transfer_line.cost_per_unit = weighted

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
    receiver: User | None = None,
) -> StockTransfer:
    """Receive at the destination: open layers there at the dispatched costs."""
    from app.dependencies import user_can_access_branch

    try:
        # Row lock first: two receivers of one transfer cannot both pass the
        # status check.
        locked = await db.execute(
            select(StockTransfer.id)
            .where(
                StockTransfer.id == transfer_id,
                StockTransfer.business_id == business_id,
                StockTransfer.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if locked.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Stock transfer not found",
            )
        transfer = (
            await db.execute(
                select(StockTransfer)
                .where(StockTransfer.id == transfer_id)
                .options(selectinload(StockTransfer.lines))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        if transfer.status != "in_transit":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transfer must be in_transit to receive",
            )
        if receiver is not None and not user_can_access_branch(
            receiver, transfer.dest_branch_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only staff of the destination branch can receive this transfer",
            )

        now = _now()
        for line in transfer.lines:
            if line.deleted_at is not None:
                continue
            slices = (
                await db.execute(
                    select(StockMovement)
                    .where(
                        StockMovement.business_id == business_id,
                        StockMovement.movement_type
                        == StockMovementTypeEnum.transfer_out.value,
                        StockMovement.reference_type
                        == ReferenceTypeEnum.stock_transfer_line.value,
                        StockMovement.reference_id == line.id,
                        StockMovement.deleted_at.is_(None),
                    )
                    .order_by(
                        StockMovement.movement_sequence.asc().nulls_last(),
                        StockMovement.created_at.asc(),
                        StockMovement.id.asc(),
                    )
                )
            ).scalars().all()
            for index, out in enumerate(slices):
                qty = abs(Decimal(str(out.qty)))
                cost = Decimal(str(out.cost_per_unit))
                layer = await create_fifo_layer(
                    db,
                    business_id=business_id,
                    branch_id=transfer.dest_branch_id,
                    product_id=line.product_id,
                    variation_id=line.variation_id,
                    qty=qty,
                    cost_per_unit=cost,
                    created_by=received_by,
                    source="transfer",
                    # +index microseconds keeps the dispatched FIFO order on ties
                    created_at=now + timedelta(microseconds=index),
                    batch_number=out.batch_number,
                    expiry_date=out.expiry_date,
                )
                await create_stock_movement(
                    db=db,
                    business_id=business_id,
                    branch_id=transfer.dest_branch_id,
                    product_id=line.product_id,
                    variation_id=line.variation_id,
                    movement_type=StockMovementTypeEnum.transfer_in,
                    qty=qty,
                    cost_per_unit=cost,
                    reference_type=ReferenceTypeEnum.stock_transfer_line,
                    reference_id=line.id,
                    created_by=received_by,
                    purchase_line_id=layer.id,
                    batch_number=out.batch_number,
                    expiry_date=out.expiry_date,
                    movement_at=now,
                    movement_sequence=index,
                )
            line.qty_received = line.qty
            line.updated_at = now

        transfer.status = "received"
        transfer.received_at = now
        transfer.updated_by = received_by
        transfer.updated_at = now

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_stock_transfer_by_id(db, transfer_id, business_id)
