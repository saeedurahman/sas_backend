"""Sale transaction services."""

import json
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
    PaymentStatusEnum,
    ReferenceTypeEnum,
    RegisterTxTypeEnum,
    SaleStatusEnum,
    StockMovementTypeEnum,
)
from app.models.product import Product
from app.models.register import RegisterTransaction
from app.models.sales import (
    Customer,
    DiscountScheme,
    Sale,
    SaleLine,
    SalePayment,
    SaleReturn,
    SaleReturnLine,
)
from app.models.user import User
from app.schemas.sales import (
    CreateSaleRequest,
    SalePricePreviewLineResponse,
    SalePricePreviewResponse,
)
from app.services.invoice_service import _round2
from app.services.customer_service import (
    create_ledger_entry,
    get_customer_balance,
    get_customer_by_id,
)
from app.services.discount_engine import (
    apply_discount_scheme,
    has_manual_discount,
    merge_line_discount_inputs,
)
from app.services.price_service import get_product_price
from app.services.fifo_service import (
    allocate_sale_line_fifo,
    create_sale_line_stock_movements,
    maybe_notify_fifo_shortfall,
    restore_sale_line_inventory,
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


_APPLY_DISCOUNT_PERMISSION = "sales.apply_discount"
_OVERRIDE_PRICE_PERMISSION = "sales.override_price"


async def _enforce_sale_pricing_permissions(
    db: AsyncSession,
    business_id: UUID,
    data: CreateSaleRequest,
    permission_keys: set[str],
) -> None:
    """Reject manual discounts and catalog price overrides without permission."""
    if any(has_manual_discount(line) for line in data.lines):
        if _APPLY_DISCOUNT_PERMISSION not in permission_keys:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {_APPLY_DISCOUNT_PERMISSION}",
            )

    if _OVERRIDE_PRICE_PERMISSION not in permission_keys:
        for line in data.lines:
            catalog_price = await get_product_price(
                db,
                business_id,
                line.product_id,
                line.variation_id,
                data.price_list_id,
            )
            if catalog_price is None:
                continue
            if _round2(line.unit_price) != _round2(catalog_price):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission required: {_OVERRIDE_PRICE_PERMISSION}",
                )


async def compute_sale_pricing(
    db: AsyncSession,
    business_id: UUID,
    data: CreateSaleRequest,
    permission_keys: set[str],
) -> SalePricePreviewResponse:
    """Pricing-only path shared by sale preview and create_sale."""
    await verify_branch(db, data.branch_id, business_id)
    await _enforce_sale_pricing_permissions(
        db, business_id, data, permission_keys
    )

    scheme_line_discounts: list[dict[str, Decimal]] | None = None
    if data.discount_scheme_id is not None:
        scheme = await _verify_discount_scheme(
            db, data.discount_scheme_id, business_id
        )
        product_ids = list({line.product_id for line in data.lines})
        category_result = await db.execute(
            select(Product.id, Product.category_id).where(
                Product.id.in_(product_ids),
                Product.business_id == business_id,
            )
        )
        product_category_by_id = {
            row.id: row.category_id for row in category_result.all()
        }
        scheme_line_discounts = apply_discount_scheme(
            scheme,
            data.lines,
            product_category_by_id,
        )

    for line in data.lines:
        await verify_product(db, line.product_id, business_id)
        if line.variation_id is not None:
            await verify_variation(
                db, line.variation_id, line.product_id, business_id
            )

    preview_lines: list[SalePricePreviewLineResponse] = []
    computed_lines: list[dict] = []

    for line_index, line_data in enumerate(data.lines):
        scheme_inputs = (
            scheme_line_discounts[line_index]
            if scheme_line_discounts is not None
            else None
        )
        discount_pct, discount_amount = merge_line_discount_inputs(
            line_data,
            scheme_inputs,
        )
        amounts = calculate_line_total(
            qty=line_data.qty,
            unit_price=line_data.unit_price,
            discount_pct=discount_pct,
            discount_amount=discount_amount,
            tax_rate=line_data.tax_rate,
        )
        computed_lines.append(amounts)
        preview_lines.append(
            SalePricePreviewLineResponse(
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty=line_data.qty,
                unit_price=line_data.unit_price,
                line_order=line_data.line_order,
                line_subtotal=amounts["line_subtotal"],
                discount_pct=line_data.discount_pct,
                discount_amount=amounts["effective_discount"],
                tax_rate=line_data.tax_rate,
                tax_amount=amounts["tax_amount"],
                line_total=amounts["line_total"],
            )
        )

    totals = calculate_sale_totals(computed_lines)
    return SalePricePreviewResponse(
        discount_scheme_id=data.discount_scheme_id,
        subtotal=totals["subtotal"],
        total_discount=totals["total_discount"],
        total_tax=totals["total_tax"],
        total_amount=totals["total_amount"],
        lines=preview_lines,
    )


async def create_sale(
    db: AsyncSession,
    business_id: UUID,
    data: CreateSaleRequest,
    created_by: UUID,
    permission_keys: set[str],
) -> Sale:
    now = _now()
    sold_at = data.sold_at or now
    allow_negative = await get_allow_negative_stock(db, business_id)

    try:
        if data.customer_id is not None:
            await get_customer_by_id(db, data.customer_id, business_id)

        pricing = await compute_sale_pricing(
            db, business_id, data, permission_keys
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

        if not allow_negative:
            for line_data in data.lines:
                sufficient = await check_sufficient_stock(
                    db,
                    business_id,
                    data.branch_id,
                    line_data.product_id,
                    line_data.variation_id,
                    line_data.qty,
                )
                if not sufficient:
                    product = await verify_product(
                        db, line_data.product_id, business_id
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock for {product.name}",
                    )

        sale_number = await generate_document_number(
            db, business_id, "INV", Sale, Sale.sale_number
        )

        line_snapshots: list[dict] = []

        for line_index, line_data in enumerate(data.lines):
            preview_line = pricing.lines[line_index]
            cost_per_unit, consumptions, short_qty = await allocate_sale_line_fifo(
                db,
                business_id,
                line_data.product_id,
                line_data.variation_id,
                line_data.qty,
            )
            consumed_from_layers = line_data.qty - short_qty
            line_snapshots.append(
                {
                    "line_data": line_data,
                    "cost_per_unit": cost_per_unit,
                    "consumptions": consumptions,
                    "short_qty": short_qty,
                    "consumed_from_layers_qty": consumed_from_layers,
                    "effective_discount": preview_line.discount_amount,
                    "tax_amount": preview_line.tax_amount,
                }
            )

        total_amount = pricing.total_amount

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

        for sale_line, snapshot in zip(created_lines, line_snapshots, strict=True):
            await create_sale_line_stock_movements(
                db,
                business_id=business_id,
                branch_id=data.branch_id,
                product_id=sale_line.product_id,
                variation_id=sale_line.variation_id,
                sale_line_id=sale_line.id,
                consumptions=snapshot["consumptions"],
                created_by=created_by,
                movement_at=sold_at,
            )
            short_qty = snapshot["short_qty"]
            if short_qty > Decimal("0"):
                fallback_cost = next(
                    (
                        c.cost_per_unit
                        for c in reversed(snapshot["consumptions"])
                        if c.purchase_line_id is None
                    ),
                    Decimal("0"),
                )
                await maybe_notify_fifo_shortfall(
                    db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=sale_line.product_id,
                    variation_id=sale_line.variation_id,
                    sale_line_id=sale_line.id,
                    requested_qty=sale_line.qty,
                    consumed_from_layers_qty=snapshot["consumed_from_layers_qty"],
                    short_qty=short_qty,
                    fallback_cost_per_unit=fallback_cost,
                    created_by=created_by,
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


async def _log_sale_audit(
    db: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID,
    sale_id: UUID,
    old_values: dict,
    new_values: dict,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO audit_logs (
                business_id, user_id, action, table_name,
                record_id, old_values, new_values
            ) VALUES (
                :business_id, :user_id, CAST(:action AS audit_action_enum),
                'sales', :record_id,
                CAST(:old_values AS jsonb), CAST(:new_values AS jsonb)
            )
            """
        ),
        {
            "business_id": business_id,
            "user_id": user_id,
            "action": "update",
            "record_id": sale_id,
            "old_values": json.dumps(old_values),
            "new_values": json.dumps(new_values),
        },
    )


def _sale_invoice_total(sale: Sale) -> Decimal:
    total = _ZERO
    for line in sale.lines:
        if line.deleted_at is None:
            total += (
                line.qty * line.unit_price
                - line.discount_amount
                + line.tax_amount
            )
    return _round2(total)


def _sale_has_credit_payment(sale: Sale) -> bool:
    return any(
        p.payment_method == PaymentMethodEnum.credit.value
        and p.deleted_at is None
        for p in sale.payments
    )


def _sale_has_completed_payments(sale: Sale) -> bool:
    return any(
        p.deleted_at is None and p.status == PaymentStatusEnum.completed.value
        for p in sale.payments
    )


async def _sale_has_return_lines(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
) -> bool:
    result = await db.execute(
        select(func.count())
        .select_from(SaleReturnLine)
        .join(SaleReturn, SaleReturnLine.sale_return_id == SaleReturn.id)
        .where(
            SaleReturn.sale_id == sale_id,
            SaleReturn.business_id == business_id,
            SaleReturn.deleted_at.is_(None),
            SaleReturnLine.deleted_at.is_(None),
        )
    )
    return int(result.scalar_one() or 0) > 0


async def _restore_all_sale_line_inventory(
    db: AsyncSession,
    sale: Sale,
    *,
    business_id: UUID,
    restored_by: UUID,
    movement_at: datetime,
) -> None:
    for sale_line in sale.lines:
        if sale_line.deleted_at is not None:
            continue
        await restore_sale_line_inventory(
            db,
            business_id=business_id,
            branch_id=sale.branch_id,
            product_id=sale_line.product_id,
            variation_id=sale_line.variation_id,
            sale_line_id=sale_line.id,
            sale_line_qty=sale_line.qty,
            sale_line_cost_per_unit=sale_line.cost_per_unit,
            qty_to_restore=sale_line.qty,
            movement_type=StockMovementTypeEnum.sale_return,
            reference_type=ReferenceTypeEnum.sale_line,
            reference_id=sale_line.id,
            created_by=restored_by,
            movement_at=movement_at,
        )


async def _reverse_completed_sale_settlements(
    db: AsyncSession,
    sale: Sale,
    *,
    business_id: UUID,
    reversed_by: UUID,
    transacted_at: datetime,
) -> None:
    """Refund completed payments, reverse register txs, undo credit ledger."""
    invoice_total = _sale_invoice_total(sale)
    reversed_any_payment = False

    for payment in sale.payments:
        if payment.deleted_at is not None:
            continue
        if payment.status != PaymentStatusEnum.completed.value:
            continue

        payment.status = PaymentStatusEnum.refunded.value
        payment.updated_at = transacted_at
        reversed_any_payment = True

        if sale.register_shift_id is not None:
            register_tx = RegisterTransaction(
                business_id=business_id,
                register_shift_id=sale.register_shift_id,
                tx_type=RegisterTxTypeEnum.sale_return.value,
                payment_method=payment.payment_method,
                amount=payment.amount,
                reference_type=ReferenceTypeEnum.sale.value,
                reference_id=sale.id,
                transacted_at=transacted_at,
                created_by=reversed_by,
                created_at=transacted_at,
                updated_at=transacted_at,
            )
            db.add(register_tx)

    if (
        reversed_any_payment
        and sale.customer_id is not None
        and _sale_has_credit_payment(sale)
    ):
        await create_ledger_entry(
            db=db,
            business_id=business_id,
            customer_id=sale.customer_id,
            entry_type=LedgerEntryTypeEnum.refund,
            amount=invoice_total,
            created_by=reversed_by,
            reference_type=ReferenceTypeEnum.sale,
            reference_id=sale.id,
            notes="Sale settlement reversed",
        )


async def _get_sale_for_update(
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
        .with_for_update()
    )
    sale = result.scalar_one_or_none()
    if sale is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sale not found",
        )
    return sale


async def _lock_sale_lines_for_update(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
) -> None:
    """Lock all active sale lines in sorted order (before locking the sale row).

    Must run before ``_get_sale_for_update`` so void/cancel participate in the
    same lock order as return_service (lines first), avoiding sale↔line deadlock.
    """
    await db.execute(
        select(SaleLine.id)
        .where(
            SaleLine.sale_id == sale_id,
            SaleLine.business_id == business_id,
            SaleLine.deleted_at.is_(None),
        )
        .order_by(SaleLine.id)
        .with_for_update()
    )


async def void_sale(
    db: AsyncSession,
    sale_id: UUID,
    business_id: UUID,
    voided_by: UUID,
) -> Sale:
    try:
        await _lock_sale_lines_for_update(db, business_id, sale_id)
        sale = await _get_sale_for_update(db, sale_id, business_id)

        if sale.status != SaleStatusEnum.completed.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only completed sales can be voided (status is '{sale.status}')",
            )
        if sale.register_shift_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Void is not available for sales without a register shift — use Returns",
            )
        if await _sale_has_return_lines(db, business_id, sale.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot void a sale that already has return lines — use Returns",
            )

        await verify_open_register_shift(
            db,
            sale.register_shift_id,
            business_id,
            closed_detail="Shift is closed — use Returns instead of voiding this sale",
        )

        now = _now()
        old_status = sale.status

        await _restore_all_sale_line_inventory(
            db,
            sale,
            business_id=business_id,
            restored_by=voided_by,
            movement_at=now,
        )
        await _reverse_completed_sale_settlements(
            db,
            sale,
            business_id=business_id,
            reversed_by=voided_by,
            transacted_at=now,
        )

        sale.status = SaleStatusEnum.voided.value
        sale.updated_by = voided_by
        sale.updated_at = now

        await _log_sale_audit(
            db,
            business_id=business_id,
            user_id=voided_by,
            sale_id=sale.id,
            old_values={"status": old_status},
            new_values={
                "status": SaleStatusEnum.voided.value,
                "action": "void",
                "voided_at": now.isoformat(),
                "voided_by": str(voided_by),
            },
        )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_sale_by_id(db, sale_id, business_id)


async def cancel_sale(
    db: AsyncSession,
    sale_id: UUID,
    business_id: UUID,
    cancelled_by: UUID,
) -> Sale:
    try:
        await _lock_sale_lines_for_update(db, business_id, sale_id)
        sale = await _get_sale_for_update(db, sale_id, business_id)

        if sale.status in ("completed", "voided"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot cancel sale with status '{sale.status}'",
            )
        if await _sale_has_return_lines(db, business_id, sale.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot cancel a sale that already has return lines — use Returns",
            )

        now = _now()

        await _restore_all_sale_line_inventory(
            db,
            sale,
            business_id=business_id,
            restored_by=cancelled_by,
            movement_at=now,
        )
        if _sale_has_completed_payments(sale):
            await _reverse_completed_sale_settlements(
                db,
                sale,
                business_id=business_id,
                reversed_by=cancelled_by,
                transacted_at=now,
            )

        sale.status = SaleStatusEnum.cancelled.value
        sale.deleted_by = cancelled_by
        sale.updated_by = cancelled_by
        sale.updated_at = now

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_sale_by_id(db, sale_id, business_id)
