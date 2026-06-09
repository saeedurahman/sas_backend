"""Invoice data assembly for Flutter PDF and thermal printing."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.business import Business
from app.models.product import ProductVariation
from app.models.sales import Sale, SaleLine
from app.models.user import User
from app.schemas.invoice import (
    BusinessInfoForInvoice,
    CustomerInfoForInvoice,
    InvoiceData,
    InvoiceLineItem,
    InvoicePaymentItem,
    InvoiceSummary,
    ThermalLineItem,
    ThermalReceiptData,
)
from app.services.settings_service import get_setting_value

_ZERO = Decimal("0")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _extract_setting_value(setting: dict | None, default: Any = None) -> Any:
    if setting is None:
        return default
    if isinstance(setting, dict) and "value" in setting:
        return setting["value"]
    return default


async def _load_sale_for_invoice(
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
        .options(
            selectinload(Sale.branch),
            selectinload(Sale.business),
            selectinload(Sale.customer),
            selectinload(Sale.payments),
            selectinload(Sale.lines)
            .selectinload(SaleLine.product),
            selectinload(Sale.lines)
            .selectinload(SaleLine.variation)
            .selectinload(ProductVariation.unit),
        )
    )
    sale = result.scalar_one_or_none()
    if sale is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sale not found",
        )
    return sale


async def _get_cashier_name(
    db: AsyncSession,
    user_id: UUID,
) -> str:
    result = await db.execute(select(User.full_name).where(User.id == user_id))
    name = result.scalar_one_or_none()
    return name or "Unknown"


def _build_summary(
    lines: list[SaleLine],
    payments: list,
) -> InvoiceSummary:
    subtotal = _ZERO
    total_discount = _ZERO
    total_tax = _ZERO

    for line in lines:
        if line.deleted_at is not None:
            continue
        subtotal += line.qty * line.unit_price
        total_discount += line.discount_amount
        total_tax += line.tax_amount

    subtotal = _round2(subtotal)
    total_discount = _round2(total_discount)
    total_tax = _round2(total_tax)
    total_amount = _round2(subtotal - total_discount + total_tax)

    total_paid = _ZERO
    for payment in payments:
        if payment.deleted_at is not None:
            continue
        if payment.status == "completed":
            total_paid += payment.amount
    total_paid = _round2(total_paid)

    balance_due = _round2(max(total_amount - total_paid, _ZERO))
    change_given = _round2(max(total_paid - total_amount, _ZERO))

    return InvoiceSummary(
        subtotal=subtotal,
        total_discount=total_discount,
        total_tax=total_tax,
        total_amount=total_amount,
        total_paid=total_paid,
        balance_due=balance_due,
        change_given=change_given,
    )


async def get_invoice_data(
    db: AsyncSession,
    sale_id: UUID,
    business_id: UUID,
) -> InvoiceData:
    sale = await _load_sale_for_invoice(db, sale_id, business_id)
    business: Business = sale.business
    branch = sale.branch

    footer_setting = await get_setting_value(
        db, business_id, "receipt.footer_text", default={}
    )
    receipt_footer = _extract_setting_value(footer_setting)

    currency_setting = await get_setting_value(
        db, business_id, "business.currency", default={}
    )
    currency = _extract_setting_value(
        currency_setting, default=business.currency_code
    )

    business_info = BusinessInfoForInvoice(
        name=business.name,
        logo_url=business.logo_url,
        address=business.address_line1,
        phone=business.phone,
        email=business.email,
        tax_id=business.tax_id,
        city=business.city,
        currency=str(currency),
    )

    customer_info: CustomerInfoForInvoice | None = None
    if sale.customer is not None:
        customer_info = CustomerInfoForInvoice(
            name=sale.customer.name,
            phone=sale.customer.phone,
            address=sale.customer.address_line1,
            tax_id=sale.customer.tax_id,
        )

    active_lines = [
        line for line in sale.lines if line.deleted_at is None
    ]
    active_lines.sort(key=lambda ln: ln.line_order)

    invoice_lines: list[InvoiceLineItem] = []
    for line in active_lines:
        unit_symbol: str | None = None
        variation_name: str | None = None
        if line.variation is not None:
            variation_name = line.variation.name
            if line.variation.unit is not None:
                unit_symbol = line.variation.unit.symbol

        line_total = _round2(
            line.qty * line.unit_price - line.discount_amount + line.tax_amount
        )
        invoice_lines.append(
            InvoiceLineItem(
                line_order=line.line_order,
                product_name=line.product.name,
                variation_name=variation_name,
                qty=line.qty,
                unit=unit_symbol,
                unit_price=line.unit_price,
                discount_pct=line.discount_pct,
                discount_amount=line.discount_amount,
                tax_rate=line.tax_rate,
                tax_amount=line.tax_amount,
                line_total=line_total,
            )
        )

    active_payments = [
        p for p in sale.payments if p.deleted_at is None
    ]
    payment_items = [
        InvoicePaymentItem(
            payment_method=p.payment_method,
            amount=p.amount,
            reference_no=p.reference_no,
            paid_at=p.paid_at,
        )
        for p in active_payments
    ]

    summary = _build_summary(active_lines, active_payments)
    cashier_name = await _get_cashier_name(db, sale.created_by)

    return InvoiceData(
        sale_id=sale.id,
        sale_number=sale.sale_number,
        sale_type=sale.sale_type,
        status=sale.status,
        sold_at=sale.sold_at,
        branch_id=sale.branch_id,
        branch_name=branch.name,
        branch_address=branch.address_line1,
        branch_phone=branch.phone,
        cashier_name=cashier_name,
        business=business_info,
        customer=customer_info,
        lines=invoice_lines,
        payments=payment_items,
        summary=summary,
        receipt_footer=receipt_footer,
        printed_at=_now(),
    )


async def get_thermal_receipt_data(
    db: AsyncSession,
    sale_id: UUID,
    business_id: UUID,
) -> ThermalReceiptData:
    invoice = await get_invoice_data(db, sale_id, business_id)

    paper_setting = await get_setting_value(
        db, business_id, "receipt.paper_size", default={"value": "80mm"}
    )
    paper_size = str(_extract_setting_value(paper_setting, default="80mm"))

    thermal_lines: list[ThermalLineItem] = []
    for line in invoice.lines:
        name = line.product_name
        if line.variation_name:
            name = f"{line.product_name} ({line.variation_name})"
        thermal_lines.append(
            ThermalLineItem(
                name=name,
                qty=line.qty,
                unit_price=line.unit_price,
                line_total=line.line_total,
            )
        )

    return ThermalReceiptData(
        sale_number=invoice.sale_number,
        sold_at=invoice.sold_at,
        branch_name=invoice.branch_name,
        cashier_name=invoice.cashier_name,
        business_name=invoice.business.name,
        lines=thermal_lines,
        summary=invoice.summary,
        payments=invoice.payments,
        footer=invoice.receipt_footer,
        paper_size=paper_size,
    )
