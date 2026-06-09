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
    StockMovementTypeEnum,
)
from app.models.inventory import PurchaseLine
from app.models.sales import DiscountScheme, Sale, SaleLine, SalePayment
from app.schemas.sales import CreateSaleRequest
from app.services.customer_service import create_ledger_entry, get_customer_by_id
from app.services.pricing_engine import (
    calculate_line_total,
    calculate_sale_totals,
    determine_sale_status,
)
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
    try:
        result = await db.execute(
            text(
                "SELECT get_fifo_cost(:bid, :pid, :vid, :qty)"
            ),
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

    fallback = await db.execute(stmt)
    last_cost = fallback.scalar_one_or_none()
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
        )
        .where(*filters)
        .order_by(Sale.sold_at.desc())
        .offset(skip)
        .limit(limit)
    )

    items: list[dict] = []
    for row in result.all():
        sale = row[0]
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
                "total_amount": Decimal(str(row.total_amount)),
                "total_paid": Decimal(str(row.total_paid)),
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
