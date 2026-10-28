"""Sale return services."""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import (
    LedgerEntryTypeEnum,
    PaymentMethodEnum,
    ReferenceTypeEnum,
    RegisterTxTypeEnum,
    StockMovementTypeEnum,
)
from app.models.register import RegisterTransaction
from app.models.sales import Sale, SaleLine, SaleReturn, SaleReturnLine, SaleReturnPayment
from app.schemas.sales import CreateSaleReturnRequest
from app.services.audit_helper import log_audit
from app.services.customer_service import create_ledger_entry, lock_customer
from app.services.fifo_service import (
    create_fifo_layer,
    get_fallback_cost_per_unit,
    restore_sale_line_inventory,
)
from app.services.register_service import require_caller_shift
from app.services.stock_service import (
    _now,
    create_stock_movement,
    generate_document_number,
    lock_stock_positions,
    verify_branch,
    verify_product,
    verify_variation,
)


_ZERO = Decimal("0")
_CENT = Decimal("0.01")


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


def _share(total: Decimal, qty: Decimal, line_qty: Decimal) -> Decimal:
    """``total`` prorated to ``qty`` of ``line_qty`` units, to the cent."""
    if line_qty <= _ZERO:
        return _ZERO
    return (total * qty / line_qty).quantize(_CENT, rounding=ROUND_HALF_UP)


async def _already_returned_amounts(
    db: AsyncSession, business_id: UUID, sale_line_id: UUID
) -> tuple[Decimal, Decimal, Decimal]:
    """(qty, discount, tax) already returned against a sale line."""
    result = await db.execute(
        select(
            func.coalesce(func.sum(SaleReturnLine.qty), 0),
            func.coalesce(func.sum(SaleReturnLine.discount_amount), 0),
            func.coalesce(func.sum(SaleReturnLine.tax_amount), 0),
        )
        .join(SaleReturn, SaleReturnLine.sale_return_id == SaleReturn.id)
        .where(
            SaleReturnLine.sale_line_id == sale_line_id,
            SaleReturnLine.business_id == business_id,
            SaleReturnLine.deleted_at.is_(None),
            SaleReturn.deleted_at.is_(None),
        )
    )
    qty, discount, tax = result.one()
    return Decimal(str(qty)), Decimal(str(discount)), Decimal(str(tax))


def compute_return_line_amounts(
    *,
    return_qty: Decimal,
    original_qty: Decimal,
    original_discount: Decimal,
    original_tax: Decimal,
    returned_qty: Decimal,
    returned_discount: Decimal,
    returned_tax: Decimal,
) -> tuple[Decimal, Decimal]:
    """(discount, tax) to refund for ``return_qty`` units of a sale line.

    Pro rata on the original line; the return that clears the line takes
    whatever is left so rounding never leaves cents unrefunded or over-refunded.
    """
    if returned_qty + return_qty >= original_qty:
        return (
            max(original_discount - returned_discount, _ZERO),
            max(original_tax - returned_tax, _ZERO),
        )
    return (
        _share(original_discount, return_qty, original_qty),
        _share(original_tax, return_qty, original_qty),
    )


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

        # Same lock order as sales: shift first.
        shift = await require_caller_shift(
            db,
            business_id,
            created_by,
            branch_id=data.branch_id,
            requested_shift_id=data.register_shift_id,
            action="record a return",
        )

        # -- resolve + lock the original sale lines ---------------------------
        line_ids = sorted(
            {l.sale_line_id for l in data.lines if l.sale_line_id is not None}
        )
        original_lines: dict[UUID, SaleLine] = {}
        if line_ids:
            locked = await db.execute(
                select(SaleLine)
                .where(
                    SaleLine.id.in_(line_ids),
                    SaleLine.business_id == business_id,
                    SaleLine.deleted_at.is_(None),
                )
                .order_by(SaleLine.id)
                .with_for_update()
            )
            original_lines = {l.id: l for l in locked.scalars().all()}
            missing = [str(i) for i in line_ids if i not in original_lines]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Original sale line not found",
                )

        sale_id = data.sale_id
        for line_id, original in original_lines.items():
            if sale_id is None:
                sale_id = original.sale_id
            elif original.sale_id != sale_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="sale_line_id does not belong to sale_id",
                )

        original_sale: Sale | None = None
        if sale_id is not None:
            sale_result = await db.execute(
                select(Sale).where(
                    Sale.id == sale_id,
                    Sale.business_id == business_id,
                    Sale.deleted_at.is_(None),
                )
            )
            original_sale = sale_result.scalar_one_or_none()
            if original_sale is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Original sale not found",
                )
            if original_sale.status in ("cancelled", "voided", "draft", "held"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot return against a {original_sale.status} sale",
                )
            if original_sale.branch_id != data.branch_id:
                # Layers are per branch: stock goes back where it was sold.
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Return branch must match the original sale branch",
                )

        customer_id = data.customer_id
        if customer_id is None and original_sale is not None:
            customer_id = original_sale.customer_id
        if (
            original_sale is not None
            and data.customer_id is not None
            and original_sale.customer_id is not None
            and data.customer_id != original_sale.customer_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id does not match the original sale",
            )
        if customer_id is not None:
            await lock_customer(db, customer_id, business_id)

        # -- validate every line, derive its money from the original line -----
        pending_qty: dict[UUID, Decimal] = {}
        prepared: list[dict] = []
        for line in data.lines:
            if line.sale_line_id is not None:
                original = original_lines[line.sale_line_id]
                if line.product_id is not None and line.product_id != original.product_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="product_id does not match the original sale line",
                    )
                if line.variation_id is not None and line.variation_id != original.variation_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="variation_id does not match the original sale line",
                    )
                returned_qty, returned_disc, returned_tax = (
                    await _already_returned_amounts(db, business_id, original.id)
                )
                in_request = pending_qty.get(original.id, _ZERO)
                remaining = original.qty - returned_qty - in_request
                if line.qty > remaining:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Cannot return {line.qty} units \u2014 only "
                            f"{remaining} units remain returnable for "
                            f"this item (originally sold: {original.qty}, "
                            f"already returned: {returned_qty})"
                        ),
                    )
                discount, tax = compute_return_line_amounts(
                    return_qty=line.qty,
                    original_qty=original.qty,
                    original_discount=original.discount_amount,
                    original_tax=original.tax_amount,
                    returned_qty=returned_qty + in_request,
                    returned_discount=returned_disc,
                    returned_tax=returned_tax,
                )
                pending_qty[original.id] = in_request + line.qty
                prepared.append(
                    {
                        "request": line,
                        "original": original,
                        "product_id": original.product_id,
                        "variation_id": original.variation_id,
                        "unit_price": original.unit_price,
                        "discount": discount,
                        "tax": tax,
                    }
                )
            else:
                await verify_product(db, line.product_id, business_id)
                if line.variation_id is not None:
                    await verify_variation(
                        db, line.variation_id, line.product_id, business_id
                    )
                prepared.append(
                    {
                        "request": line,
                        "original": None,
                        "product_id": line.product_id,
                        "variation_id": line.variation_id,
                        "unit_price": line.unit_price,
                        "discount": _ZERO,
                        "tax": line.tax_amount or _ZERO,
                    }
                )

        total_refund = _ZERO
        for item in prepared:
            qty = item["request"].qty
            total_refund += (
                qty * item["unit_price"] - item["discount"] + item["tax"]
            ).quantize(_CENT, rounding=ROUND_HALF_UP)

        # -- refund payments: never more than is owed -------------------------
        paid_out = _ZERO
        for refund in data.refund_payments:
            paid_out += refund.amount
        if paid_out > total_refund:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Refund payments ({paid_out}) exceed the amount refundable "
                    f"({total_refund})"
                ),
            )
        cash_out = sum(
            (
                r.amount
                for r in data.refund_payments
                if r.payment_method != PaymentMethodEnum.credit
            ),
            _ZERO,
        )
        credit_portion = total_refund - cash_out
        if credit_portion > _ZERO and customer_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Refund payments must cover the whole refund unless the return "
                    "is linked to a customer account"
                ),
            )

        await lock_stock_positions(
            db,
            business_id,
            data.branch_id,
            [(i["product_id"], i["variation_id"]) for i in prepared],
        )

        return_number = await generate_document_number(
            db, business_id, "RET", SaleReturn, SaleReturn.return_number
        )

        sale_return = SaleReturn(
            business_id=business_id,
            branch_id=data.branch_id,
            sale_id=sale_id,
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

        for item in prepared:
            line_data = item["request"]
            original = item["original"]
            return_line = SaleReturnLine(
                business_id=business_id,
                sale_return_id=sale_return.id,
                sale_line_id=line_data.sale_line_id,
                product_id=item["product_id"],
                variation_id=item["variation_id"],
                qty=line_data.qty,
                unit_price=item["unit_price"],
                discount_amount=item["discount"],
                tax_amount=item["tax"],
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(return_line)
            await db.flush()

            if original is not None:
                await restore_sale_line_inventory(
                    db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=item["product_id"],
                    variation_id=item["variation_id"],
                    sale_line_id=original.id,
                    sale_line_qty=original.qty,
                    sale_line_cost_per_unit=original.cost_per_unit,
                    qty_to_restore=line_data.qty,
                    movement_type=StockMovementTypeEnum.sale_return,
                    reference_type=ReferenceTypeEnum.sale_return_line,
                    reference_id=return_line.id,
                    created_by=created_by,
                    movement_at=returned_at,
                )
            else:
                # No original sale to take a cost from: last known product cost,
                # held in its own layer so stock and layers stay in step.
                cost = await get_fallback_cost_per_unit(
                    db, business_id, item["product_id"], item["variation_id"]
                )
                layer = await create_fifo_layer(
                    db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=item["product_id"],
                    variation_id=item["variation_id"],
                    qty=line_data.qty,
                    cost_per_unit=cost,
                    created_by=created_by,
                    source="return",
                    created_at=returned_at,
                )
                await create_stock_movement(
                    db=db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=item["product_id"],
                    variation_id=item["variation_id"],
                    movement_type=StockMovementTypeEnum.sale_return,
                    qty=line_data.qty,
                    cost_per_unit=cost,
                    reference_type=ReferenceTypeEnum.sale_return_line,
                    reference_id=return_line.id,
                    created_by=created_by,
                    purchase_line_id=layer.id,
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
            db.add(
                RegisterTransaction(
                    business_id=business_id,
                    register_shift_id=shift.id,
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
            )

        # Only what was not paid out goes to the customer account.
        if customer_id is not None and credit_portion > _ZERO:
            await create_ledger_entry(
                db=db,
                business_id=business_id,
                customer_id=customer_id,
                entry_type=LedgerEntryTypeEnum.return_,
                amount=credit_portion,
                created_by=created_by,
                reference_type=ReferenceTypeEnum.sale_return,
                reference_id=sale_return.id,
            )

        await log_audit(
            db,
            business_id=business_id,
            user_id=created_by,
            branch_id=data.branch_id,
            action="create",
            table_name="sale_returns",
            record_id=sale_return.id,
            old_values={"sale_id": str(sale_id)},
            new_values={
                "event": "return",
                "return_number": return_number,
                "total_refund": str(total_refund),
                "cash_out": str(cash_out),
                "credit_to_account": str(credit_portion),
                "lines": [
                    {
                        "sale_line_id": str(i["request"].sale_line_id),
                        "qty": str(i["request"].qty),
                    }
                    for i in prepared
                ],
            },
        )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_sale_return_by_id(db, sale_return.id, business_id)
