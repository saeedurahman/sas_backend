"""Sale return services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import (
    LedgerEntryTypeEnum,
    ReferenceTypeEnum,
    RegisterTxTypeEnum,
    StockMovementTypeEnum,
)
from app.models.register import RegisterTransaction
from app.models.sales import Sale, SaleLine, SaleReturn, SaleReturnLine, SaleReturnPayment
from app.schemas.sales import CreateSaleReturnRequest
from app.services.customer_service import create_ledger_entry, get_customer_by_id
from app.services.fifo_service import restore_sale_line_inventory
from app.services.stock_service import (
    _now,
    create_stock_movement,
    generate_document_number,
    verify_branch,
    verify_product,
    verify_variation,
)


_ZERO = Decimal("0")


async def get_returned_qty_by_sale_line_ids(
    db: AsyncSession,
    business_id: UUID,
    sale_line_ids: list[UUID],
) -> dict[UUID, Decimal]:
    """Sum non-deleted return line qty per sale line across all returns."""
    if not sale_line_ids:
        return {}

    result = await db.execute(
        select(
            SaleReturnLine.sale_line_id,
            func.coalesce(func.sum(SaleReturnLine.qty), 0),
        )
        .join(SaleReturn, SaleReturnLine.sale_return_id == SaleReturn.id)
        .where(
            SaleReturnLine.sale_line_id.in_(sale_line_ids),
            SaleReturnLine.business_id == business_id,
            SaleReturnLine.deleted_at.is_(None),
            SaleReturn.business_id == business_id,
            SaleReturn.deleted_at.is_(None),
        )
        .group_by(SaleReturnLine.sale_line_id)
    )

    returned: dict[UUID, Decimal] = {line_id: _ZERO for line_id in sale_line_ids}
    for sale_line_id, qty in result.all():
        if sale_line_id is not None:
            returned[sale_line_id] = Decimal(str(qty))
    return returned


async def _sum_returned_qty_for_sale_line(
    db: AsyncSession,
    business_id: UUID,
    sale_line_id: UUID,
) -> Decimal:
    result = await db.execute(
        select(func.coalesce(func.sum(SaleReturnLine.qty), 0))
        .select_from(SaleReturnLine)
        .join(SaleReturn, SaleReturnLine.sale_return_id == SaleReturn.id)
        .where(
            SaleReturnLine.sale_line_id == sale_line_id,
            SaleReturnLine.business_id == business_id,
            SaleReturnLine.deleted_at.is_(None),
            SaleReturn.business_id == business_id,
            SaleReturn.deleted_at.is_(None),
        )
    )
    return Decimal(str(result.scalar_one()))


async def get_sale_returns(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[SaleReturn], int]:
    filters = [
        SaleReturn.business_id == business_id,
        SaleReturn.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(SaleReturn.branch_id == branch_id)

    count_result = await db.execute(
        select(func.count()).select_from(SaleReturn).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(SaleReturn)
        .where(*filters)
        .options(
            selectinload(SaleReturn.lines),
            selectinload(SaleReturn.payments),
        )
        .order_by(SaleReturn.returned_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def get_sale_return_by_id(
    db: AsyncSession,
    return_id: UUID,
    business_id: UUID,
) -> SaleReturn:
    result = await db.execute(
        select(SaleReturn)
        .where(
            SaleReturn.id == return_id,
            SaleReturn.business_id == business_id,
            SaleReturn.deleted_at.is_(None),
        )
        .options(
            selectinload(SaleReturn.lines),
            selectinload(SaleReturn.payments),
        )
    )
    sale_return = result.scalar_one_or_none()
    if sale_return is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sale return not found",
        )
    return sale_return


async def create_sale_return(
    db: AsyncSession,
    business_id: UUID,
    data: CreateSaleReturnRequest,
    created_by: UUID,
) -> SaleReturn:
    now = _now()
    returned_at = data.returned_at or now

    try:
        await verify_branch(db, data.branch_id, business_id)

        original_sale: Sale | None = None
        if data.sale_id is not None:
            sale_result = await db.execute(
                select(Sale)
                .where(
                    Sale.id == data.sale_id,
                    Sale.business_id == business_id,
                    Sale.deleted_at.is_(None),
                )
                .options(selectinload(Sale.lines))
            )
            original_sale = sale_result.scalar_one_or_none()
            if original_sale is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Original sale not found",
                )
            if original_sale.status in ("cancelled", "voided"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot return against a cancelled or voided sale",
                )

        customer_id = data.customer_id
        if customer_id is None and original_sale is not None:
            customer_id = original_sale.customer_id

        if customer_id is not None:
            await get_customer_by_id(db, customer_id, business_id)

        sale_line_ids_to_lock = sorted(
            {
                line.sale_line_id
                for line in data.lines
                if line.sale_line_id is not None
            }
        )
        if sale_line_ids_to_lock:
            await db.execute(
                select(SaleLine.id)
                .where(
                    SaleLine.id.in_(sale_line_ids_to_lock),
                    SaleLine.business_id == business_id,
                    SaleLine.deleted_at.is_(None),
                )
                .with_for_update()
            )

        if original_sale is not None:
            await db.refresh(original_sale, attribute_names=["status"])
            if original_sale.status in ("cancelled", "voided"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot return against a cancelled or voided sale",
                )

        pending_return_qty: dict[UUID, Decimal] = {}

        for line in data.lines:
            await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )

            if line.sale_line_id is not None:
                sl_result = await db.execute(
                    select(SaleLine).where(
                        SaleLine.id == line.sale_line_id,
                        SaleLine.business_id == business_id,
                        SaleLine.deleted_at.is_(None),
                    )
                )
                original_line = sl_result.scalar_one_or_none()
                if original_line is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Original sale line not found",
                    )

                already_returned_qty = await _sum_returned_qty_for_sale_line(
                    db, business_id, line.sale_line_id
                )
                pending_in_request = pending_return_qty.get(
                    line.sale_line_id, _ZERO
                )
                max_returnable = (
                    original_line.qty - already_returned_qty - pending_in_request
                )
                if line.qty > max_returnable:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Cannot return {line.qty} units — only "
                            f"{max_returnable} units remain returnable for "
                            f"this item (originally sold: {original_line.qty}, "
                            f"already returned: {already_returned_qty})"
                        ),
                    )
                pending_return_qty[line.sale_line_id] = (
                    pending_in_request + line.qty
                )

        return_number = await generate_document_number(
            db, business_id, "RET", SaleReturn, SaleReturn.return_number
        )

        sale_return = SaleReturn(
            business_id=business_id,
            branch_id=data.branch_id,
            sale_id=data.sale_id,
            customer_id=customer_id,
            return_number=return_number,
            returned_at=returned_at,
            reason=data.reason,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(sale_return)
        await db.flush()

        total_refund = Decimal("0")
        created_return_lines: list[SaleReturnLine] = []

        for line_data in data.lines:
            return_line = SaleReturnLine(
                business_id=business_id,
                sale_return_id=sale_return.id,
                sale_line_id=line_data.sale_line_id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty=line_data.qty,
                unit_price=line_data.unit_price,
                tax_amount=line_data.tax_amount,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(return_line)
            await db.flush()
            created_return_lines.append(return_line)

            line_refund = line_data.qty * line_data.unit_price + line_data.tax_amount
            total_refund += line_refund

            if line_data.sale_line_id is not None:
                sl_result = await db.execute(
                    select(SaleLine).where(
                        SaleLine.id == line_data.sale_line_id,
                        SaleLine.business_id == business_id,
                        SaleLine.deleted_at.is_(None),
                    )
                )
                original_line = sl_result.scalar_one_or_none()
                if original_line is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Original sale line not found",
                    )
                await restore_sale_line_inventory(
                    db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=line_data.product_id,
                    variation_id=line_data.variation_id,
                    sale_line_id=original_line.id,
                    sale_line_qty=original_line.qty,
                    sale_line_cost_per_unit=original_line.cost_per_unit,
                    qty_to_restore=line_data.qty,
                    movement_type=StockMovementTypeEnum.sale_return,
                    reference_type=ReferenceTypeEnum.sale_return_line,
                    reference_id=return_line.id,
                    created_by=created_by,
                    movement_at=returned_at,
                )
            else:
                await create_stock_movement(
                    db=db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=line_data.product_id,
                    variation_id=line_data.variation_id,
                    movement_type=StockMovementTypeEnum.sale_return,
                    qty=line_data.qty,
                    cost_per_unit=Decimal("0"),
                    reference_type=ReferenceTypeEnum.sale_return_line,
                    reference_id=return_line.id,
                    created_by=created_by,
                    movement_at=returned_at,
                )

        for refund in data.refund_payments:
            payment = SaleReturnPayment(
                business_id=business_id,
                sale_return_id=sale_return.id,
                payment_method=refund.payment_method.value,
                amount=refund.amount,
                status="completed",
                refunded_at=returned_at,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(payment)

            if data.register_shift_id is not None:
                register_tx = RegisterTransaction(
                    business_id=business_id,
                    register_shift_id=data.register_shift_id,
                    tx_type=RegisterTxTypeEnum.sale_return.value,
                    payment_method=payment.payment_method,
                    amount=payment.amount,
                    reference_type=ReferenceTypeEnum.sale_return.value,
                    reference_id=sale_return.id,
                    transacted_at=returned_at,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                db.add(register_tx)

        if customer_id is not None and total_refund > 0:
            await create_ledger_entry(
                db=db,
                business_id=business_id,
                customer_id=customer_id,
                entry_type=LedgerEntryTypeEnum.return_,
                amount=total_refund,
                created_by=created_by,
                reference_type=ReferenceTypeEnum.sale_return,
                reference_id=sale_return.id,
            )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_sale_return_by_id(db, sale_return.id, business_id)
