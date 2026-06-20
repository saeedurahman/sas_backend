"""Sale transaction services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import (
    LedgerEntryTypeEnum,
    PaymentMethodEnum,
    ReferenceTypeEnum,
    RegisterTxTypeEnum,
    StockMovementTypeEnum,
)
from app.models.inventory import PurchaseLine
from app.models.register import RegisterTransaction
from app.models.sales import Customer, DiscountScheme, Sale, SaleLine, SalePayment
from app.models.user import User
from app.schemas.sales import CreateSaleRequest
from app.services.invoice_service import _round2
from app.services.customer_service import (
    create_ledger_entry,
    get_customer_balance,
    get_customer_by_id,
)
from app.services.register_service import verify_open_register_shift
from app.services.pricing_engine import (
    calculate_line_total,
    calculate_sale_totals,
    determine_sale_status,
)
from app.services.return_service import get_returned_qty_by_sale_line_ids
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


def _sale_detail_options():
    return (
        selectinload(Sale.lines),
        selectinload(Sale.payments),
        selectinload(Sale.customer),
        selectinload(Sale.discount_scheme),
    )


async def _get_fifo_cost_per_unit(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    qty: Decimal,
) -> Decimal:
    # Primary path: the get_fifo_cost stored proc computes true weighted FIFO
    # cost. Wrap it in a SAVEPOINT so that if the proc errors at the Postgres
    # level (e.g. no purchase history to aggregate), the savepoint rolls back
    # and the outer transaction stays usable for the fallback query below —
    # otherwise the connection is left in an aborted state and every
    # subsequent statement fails with InFailedSQLTransactionError.
    try:
        async with db.begin_nested():
            result = await db.execute(
                text("SELECT get_fifo_cost(:bid, :pid, :vid, :qty)"),
                {
                    "bid": business_id,
                    "pid": product_id,
                    "vid": variation_id,
                    "qty": qty,
                },
            )
            cost = result.scalar_one_or_none()
        if cost is not None:
            return Decimal(str(cost))
    except Exception:
        pass

    # Fallback: most recent recorded purchase cost for this product/variation.
    # Returns NULL (→ Decimal("0")) for products never received via a purchase.
    stmt = (
        select(PurchaseLine.cost_per_unit)
        .where(
            PurchaseLine.business_id == business_id,
            PurchaseLine.product_id == product_id,
            PurchaseLine.deleted_at.is_(None),
        )
        .order_by(PurchaseLine.created_at.desc())
        .limit(1)
    )
    if variation_id is None:
        stmt = stmt.where(PurchaseLine.variation_id.is_(None))
    else:
        stmt = stmt.where(PurchaseLine.variation_id == variation_id)

    last_cost = (await db.execute(stmt)).scalar_one_or_none()
    if last_cost is not None:
        return Decimal(str(last_cost))
    return Decimal("0")


async def _verify_discount_scheme(
    db: AsyncSession,
    scheme_id: UUID,
    business_id: UUID,
) -> DiscountScheme:
    result = await db.execute(
        select(DiscountScheme).where(
            DiscountScheme.id == scheme_id,
            DiscountScheme.business_id == business_id,
            DiscountScheme.deleted_at.is_(None),
            DiscountScheme.is_active.is_(True),
        )
    )
    scheme = result.scalar_one_or_none()
    if scheme is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Discount scheme not found or inactive",
        )
    now = _now()
    if scheme.valid_from is not None and now < scheme.valid_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Discount scheme is not yet valid",
        )
    if scheme.valid_to is not None and now > scheme.valid_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Discount scheme has expired",
        )
    return scheme


_ZERO = Decimal("0")


async def _fetch_payment_methods_by_sale(
    db: AsyncSession,
    business_id: UUID,
    sale_ids: list[UUID],
) -> dict[UUID, list[str]]:
    if not sale_ids:
        return {}

    result = await db.execute(
        select(SalePayment.sale_id, SalePayment.payment_method)
        .where(
            SalePayment.sale_id.in_(sale_ids),
            SalePayment.business_id == business_id,
            SalePayment.deleted_at.is_(None),
            SalePayment.status == "completed",
        )
        .distinct()
        .order_by(SalePayment.sale_id, SalePayment.payment_method)
    )

    methods_by_sale: dict[UUID, list[str]] = {sale_id: [] for sale_id in sale_ids}
    for sale_id, payment_method in result.all():
        methods_by_sale[sale_id].append(payment_method)
    return methods_by_sale


async def get_sales(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    customer_id: UUID | None = None,
    status_filter: str | None = None,
    sale_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict], int]:
    line_total = (
        SaleLine.qty * SaleLine.unit_price
        - SaleLine.discount_amount
        + SaleLine.tax_amount
    )
    total_amount_subq = (
        select(func.coalesce(func.sum(line_total), 0))
        .where(
            SaleLine.sale_id == Sale.id,
            SaleLine.business_id == business_id,
            SaleLine.deleted_at.is_(None),
        )
        .correlate(Sale)
        .scalar_subquery()
    )
    total_paid_subq = (
        select(func.coalesce(func.sum(SalePayment.amount), 0))
        .where(
            SalePayment.sale_id == Sale.id,
            SalePayment.business_id == business_id,
            SalePayment.deleted_at.is_(None),
            SalePayment.status == "completed",
        )
        .correlate(Sale)
        .scalar_subquery()
    )
    item_count_subq = (
        select(func.count())
        .select_from(SaleLine)
        .where(
            SaleLine.sale_id == Sale.id,
            SaleLine.business_id == business_id,
            SaleLine.deleted_at.is_(None),
        )
        .correlate(Sale)
        .scalar_subquery()
    )

    filters = [
        Sale.business_id == business_id,
        Sale.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(Sale.branch_id == branch_id)
    if customer_id is not None:
        filters.append(Sale.customer_id == customer_id)
    if status_filter is not None:
        filters.append(Sale.status == status_filter)
    if sale_type is not None:
        filters.append(Sale.sale_type == sale_type)
    if date_from is not None:
        filters.append(Sale.sold_at >= date_from)
    if date_to is not None:
        filters.append(Sale.sold_at <= date_to)
    if search:
        pattern = f"%{search}%"
        filters.append(
            or_(Sale.sale_number.ilike(pattern), Sale.notes.ilike(pattern))
        )

    count_result = await db.execute(
        select(func.count()).select_from(Sale).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(
            Sale,
            total_amount_subq.label("total_amount"),
            total_paid_subq.label("total_paid"),
            item_count_subq.label("item_count"),
            Customer.name.label("customer_name"),
            User.full_name.label("cashier_name"),
        )
        .outerjoin(Customer, Sale.customer_id == Customer.id)
        .join(User, Sale.created_by == User.id)
        .where(*filters)
        .order_by(Sale.sold_at.desc())
        .offset(skip)
        .limit(limit)
    )

    rows = result.all()
    sale_ids = [row[0].id for row in rows]
    payment_methods_by_sale = await _fetch_payment_methods_by_sale(
        db, business_id, sale_ids
    )

    items: list[dict] = []
    for row in rows:
        sale = row[0]
        total_amount = _round2(Decimal(str(row.total_amount)))
        total_paid = _round2(Decimal(str(row.total_paid)))
        balance_due = _round2(max(total_amount - total_paid, _ZERO))
        items.append(
            {
                "id": sale.id,
                "business_id": sale.business_id,
                "branch_id": sale.branch_id,
                "customer_id": sale.customer_id,
                "sale_number": sale.sale_number,
                "sale_type": sale.sale_type,
                "status": sale.status,
                "sold_at": sale.sold_at,
                "total_amount": total_amount,
                "total_paid": total_paid,
                "balance_due": balance_due,
                "customer_name": row.customer_name,
                "cashier_name": row.cashier_name,
                "item_count": int(row.item_count),
                "payment_methods": payment_methods_by_sale.get(sale.id, []),
            }
        )
    return items, total


async def get_sale_by_id(
    db: AsyncSession,
    sale_id: UUID,
    business_id: UUID,
) -> Sale:
    result = await db.execute(
        select(Sale)
        .where(
            Sale.id == sale_id,
            Sale.business_id == business_id,
            Sale.deleted_at.is_(None),
        )
        .options(*_sale_detail_options())
    )
    sale = result.scalar_one_or_none()
    if sale is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sale not found",
        )

    active_line_ids = [
        line.id for line in sale.lines if line.deleted_at is None
    ]
    returned_by_line = await get_returned_qty_by_sale_line_ids(
        db, business_id, active_line_ids
    )
    for line in sale.lines:
        if line.deleted_at is None:
            line.returned_qty = returned_by_line.get(line.id, Decimal("0"))
        else:
            line.returned_qty = Decimal("0")

    return sale


async def create_sale(
    db: AsyncSession,
    business_id: UUID,
    data: CreateSaleRequest,
    created_by: UUID,
) -> Sale:
    now = _now()
    sold_at = data.sold_at or now
    allow_negative = await get_allow_negative_stock(db, business_id)

    try:
        await verify_branch(db, data.branch_id, business_id)

        if data.customer_id is not None:
            await get_customer_by_id(db, data.customer_id, business_id)

        if data.discount_scheme_id is not None:
            await _verify_discount_scheme(
                db, data.discount_scheme_id, business_id
            )

        if data.register_shift_id is not None:
            await verify_open_register_shift(
                db,
                data.register_shift_id,
                business_id,
                closed_detail="Cannot record sale — shift is not open",
            )

        credit_payments = [
            p for p in data.payments
            if p.payment_method == PaymentMethodEnum.credit
        ]
        if credit_payments:
            if data.customer_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="customer_id is required for credit payments",
                )
            customer = await get_customer_by_id(
                db, data.customer_id, business_id
            )
            credit_amount_this_sale = sum(
                (p.amount for p in credit_payments),
                Decimal("0"),
            )
            if customer.credit_limit > Decimal("0"):
                current_balance = await get_customer_balance(
                    db, data.customer_id, business_id
                )
                projected_balance = current_balance - credit_amount_this_sale
                if (
                    projected_balance < Decimal("0")
                    and abs(projected_balance) > customer.credit_limit
                ):
                    if current_balance < Decimal("0"):
                        available = customer.credit_limit - abs(current_balance)
                    else:
                        available = customer.credit_limit
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            "Credit limit exceeded. Customer's available "
                            f"credit is Rs. {available}, but this sale "
                            f"requires Rs. {credit_amount_this_sale} on credit."
                        ),
                    )

        for line in data.lines:
            await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )

        sale_number = await generate_document_number(
            db, business_id, "INV", Sale, Sale.sale_number
        )

        computed_lines: list[dict] = []
        line_snapshots: list[dict] = []

        for line_data in data.lines:
            cost_per_unit = await _get_fifo_cost_per_unit(
                db,
                business_id,
                line_data.product_id,
                line_data.variation_id,
                line_data.qty,
            )
            amounts = calculate_line_total(
                qty=line_data.qty,
                unit_price=line_data.unit_price,
                discount_pct=line_data.discount_pct,
                discount_amount=line_data.discount_amount,
                tax_rate=line_data.tax_rate,
            )
            computed_lines.append(amounts)
            line_snapshots.append(
                {
                    "line_data": line_data,
                    "cost_per_unit": cost_per_unit,
                    "effective_discount": amounts["effective_discount"],
                    "tax_amount": amounts["tax_amount"],
                }
            )

        totals = calculate_sale_totals(computed_lines)
        total_amount = totals["total_amount"]

        total_paid = sum(
            (p.amount for p in data.payments),
            Decimal("0"),
        )
        sale_status = determine_sale_status(total_amount, total_paid)

        sale = Sale(
            business_id=business_id,
            branch_id=data.branch_id,
            customer_id=data.customer_id,
            price_list_id=data.price_list_id,
            sale_number=sale_number,
            sale_type=data.sale_type.value,
            status=sale_status.value,
            sold_at=sold_at,
            notes=data.notes,
            discount_scheme_id=data.discount_scheme_id,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(sale)
        await db.flush()

        if data.register_shift_id is not None:
            sale.register_shift_id = data.register_shift_id

        created_lines: list[SaleLine] = []
        for snapshot in line_snapshots:
            line_data = snapshot["line_data"]
            sale_line = SaleLine(
                business_id=business_id,
                sale_id=sale.id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty=line_data.qty,
                unit_price=line_data.unit_price,
                discount_pct=line_data.discount_pct,
                discount_amount=snapshot["effective_discount"],
                tax_rate=line_data.tax_rate,
                tax_amount=snapshot["tax_amount"],
                cost_per_unit=snapshot["cost_per_unit"],
                notes=line_data.notes,
                line_order=line_data.line_order,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(sale_line)
            await db.flush()
            created_lines.append(sale_line)

        for payment_data in data.payments:
            paid_at = payment_data.paid_at or now
            payment = SalePayment(
                business_id=business_id,
                sale_id=sale.id,
                payment_method=payment_data.payment_method.value,
                amount=payment_data.amount,
                status="completed",
                reference_no=payment_data.reference_no,
                paid_at=paid_at,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(payment)

            if (
                data.register_shift_id is not None
                and payment.status == "completed"
            ):
                register_tx = RegisterTransaction(
                    business_id=business_id,
                    register_shift_id=data.register_shift_id,
                    tx_type=RegisterTxTypeEnum.sale.value,
                    payment_method=payment.payment_method,
                    amount=payment.amount,
                    reference_type=ReferenceTypeEnum.sale.value,
                    reference_id=sale.id,
                    transacted_at=paid_at,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                db.add(register_tx)

        for sale_line in created_lines:
            if not allow_negative:
                sufficient = await check_sufficient_stock(
                    db,
                    business_id,
                    data.branch_id,
                    sale_line.product_id,
                    sale_line.variation_id,
                    sale_line.qty,
                )
                if not sufficient:
                    product = await verify_product(
                        db, sale_line.product_id, business_id
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock for {product.name}",
                    )

            await create_stock_movement(
                db=db,
                business_id=business_id,
                branch_id=data.branch_id,
                product_id=sale_line.product_id,
                variation_id=sale_line.variation_id,
                movement_type=StockMovementTypeEnum.sale,
                qty=-sale_line.qty,
                cost_per_unit=sale_line.cost_per_unit,
                reference_type=ReferenceTypeEnum.sale_line,
                reference_id=sale_line.id,
                created_by=created_by,
                movement_at=sold_at,
            )

        has_credit = any(
            p.payment_method == PaymentMethodEnum.credit for p in data.payments
        )
        if data.customer_id is not None and has_credit:
            await create_ledger_entry(
                db=db,
                business_id=business_id,
                customer_id=data.customer_id,
                entry_type=LedgerEntryTypeEnum.sale,
                amount=-total_amount,
                created_by=created_by,
                reference_type=ReferenceTypeEnum.sale,
                reference_id=sale.id,
            )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_sale_by_id(db, sale.id, business_id)


async def cancel_sale(
    db: AsyncSession,
    sale_id: UUID,
    business_id: UUID,
    cancelled_by: UUID,
) -> Sale:
    sale = await get_sale_by_id(db, sale_id, business_id)

    if sale.status in ("completed", "voided"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel sale with status '{sale.status}'",
        )

    now = _now()

    try:
        for sale_line in sale.lines:
            if sale_line.deleted_at is not None:
                continue
            await create_stock_movement(
                db=db,
                business_id=business_id,
                branch_id=sale.branch_id,
                product_id=sale_line.product_id,
                variation_id=sale_line.variation_id,
                movement_type=StockMovementTypeEnum.sale_return,
                qty=sale_line.qty,
                cost_per_unit=sale_line.cost_per_unit,
                reference_type=ReferenceTypeEnum.sale_line,
                reference_id=sale_line.id,
                created_by=cancelled_by,
                movement_at=now,
            )

        sale.status = "cancelled"
        sale.deleted_by = cancelled_by
        sale.updated_by = cancelled_by
        sale.updated_at = now

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return await get_sale_by_id(db, sale_id, business_id)
