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
from app.models.product import Barcode, Product, ProductVariation
from app.models.receipt_layout import SaleLineBatch
from app.models.register import CashRegister, RegisterShift
from app.models.sales import CustomerLedger, Sale, SaleLine
from app.models.user import User
from app.schemas.invoice import (
    BusinessInfoForInvoice,
    CustomerInfoForInvoice,
    InvoiceData,
    InvoiceLineBatch,
    InvoiceLineItem,
    InvoicePaymentItem,
    InvoiceSummary,
    InvoiceTaxBreakdownItem,
    ThermalLineItem,
    ThermalReceiptData,
)
from app.services.settings_service import get_setting_value
from app.services.tax_settings import TaxSettings, load_tax_settings

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
            .selectinload(SaleLine.product)
            .selectinload(Product.base_unit),
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


def visible_variation_name(name: str | None) -> str | None:
    """A variation is only worth printing when it has a real name ("Default" is the placeholder)."""
    if name is None:
        return None
    cleaned = name.strip()
    if not cleaned or cleaned.casefold() == "default":
        return None
    return cleaned


async def _line_batches(
    db: AsyncSession, business_id: UUID, line_ids: list[UUID]
) -> dict[UUID, list[InvoiceLineBatch]]:
    if not line_ids:
        return {}
    rows = (
        await db.execute(
            select(SaleLineBatch)
            .where(SaleLineBatch.business_id == business_id, SaleLineBatch.sale_line_id.in_(line_ids))
            .order_by(SaleLineBatch.created_at, SaleLineBatch.id)
        )
    ).scalars().all()
    out: dict[UUID, list[InvoiceLineBatch]] = {}
    for row in rows:
        out.setdefault(row.sale_line_id, []).append(
            InvoiceLineBatch(batch_no=row.batch_no, expiry_date=row.expiry_date, qty=row.qty)
        )
    return out


async def _primary_barcodes(
    db: AsyncSession, business_id: UUID, product_ids: list[UUID]
) -> dict[UUID, str]:
    if not product_ids:
        return {}
    rows = (
        await db.execute(
            select(Barcode.product_id, Barcode.barcode)
            .where(
                Barcode.business_id == business_id,
                Barcode.product_id.in_(product_ids),
                Barcode.deleted_at.is_(None),
            )
            .order_by(Barcode.is_primary.desc(), Barcode.created_at)
        )
    ).all()
    out: dict[UUID, str] = {}
    for product_id, code in rows:
        out.setdefault(product_id, code)
    return out


async def _register_name(db: AsyncSession, business_id: UUID, shift_id: UUID | None) -> str | None:
    if shift_id is None:
        return None
    return (
        await db.execute(
            select(CashRegister.name)
            .join(RegisterShift, RegisterShift.cash_register_id == CashRegister.id)
            .where(RegisterShift.id == shift_id, RegisterShift.business_id == business_id)
        )
    ).scalar_one_or_none()


async def _customer_balances(
    db: AsyncSession, business_id: UUID, sale: Sale
) -> tuple[Decimal | None, Decimal | None]:
    """(previous, after) as amounts the customer owes: before this sale, and including it."""
    if sale.customer_id is None:
        return None, None
    rows = (
        await db.execute(
            select(CustomerLedger.amount, CustomerLedger.reference_id)
            .where(
                CustomerLedger.business_id == business_id,
                CustomerLedger.customer_id == sale.customer_id,
                CustomerLedger.deleted_at.is_(None),
                CustomerLedger.entry_at <= sale.sold_at,
            )
        )
    ).all()
    own = _ZERO
    others = _ZERO
    for amount, reference_id in rows:
        if reference_id == sale.id:
            own += Decimal(str(amount))
        else:
            others += Decimal(str(amount))
    return _round2(-others), _round2(-(others + own))


def tax_breakdown_for_lines(lines: list[SaleLine], label: str) -> list[InvoiceTaxBreakdownItem]:
    """Tax grouped by rate: stored net and tax of the lines that carry tax."""
    groups: dict[Decimal, list[Decimal]] = {}
    for line in lines:
        if line.deleted_at is not None or line.tax_amount == _ZERO:
            continue
        acc = groups.setdefault(Decimal(str(line.tax_rate)), [_ZERO, _ZERO])
        acc[0] += line.net_amount
        acc[1] += line.tax_amount
    return [
        InvoiceTaxBreakdownItem(
            tax_label=label,
            tax_rate=rate,
            net_amount=_round2(net),
            tax_amount=_round2(tax),
        )
        for rate, (net, tax) in sorted(groups.items())
    ]


def _build_summary(
    sale: Sale,
    lines: list[SaleLine],
    payments: list,
    cfg: TaxSettings,
) -> InvoiceSummary:
    subtotal = _ZERO
    total_discount = _ZERO
    net_amount = _ZERO
    total_tax = _ZERO

    for line in lines:
        if line.deleted_at is not None:
            continue
        subtotal += _round2(line.qty * line.unit_price)
        total_discount += line.discount_amount
        net_amount += line.net_amount
        total_tax += line.tax_amount

    subtotal = _round2(subtotal)
    total_discount = _round2(total_discount)
    net_amount = _round2(net_amount)
    total_tax = _round2(total_tax)
    exact_total = _round2(net_amount + total_tax)
    rounding_amount = _round2(_dec(sale.rounding_amount or 0))
    total_amount = _round2(exact_total + rounding_amount)

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
        tax_inclusive=sale.tax_inclusive,
        tax_label=sale.tax_label,
        tax_rounding_mode=sale.tax_rounding_mode,
        show_tax_breakdown=bool(
            cfg.enabled and cfg.show_breakdown_on_receipt and total_tax > _ZERO
        ),
        subtotal=subtotal,
        total_discount=total_discount,
        net_amount=net_amount,
        total_tax=total_tax,
        exact_total=exact_total,
        rounding_amount=rounding_amount,
        total_amount=total_amount,
        tax_breakdown=tax_breakdown_for_lines(lines, sale.tax_label),
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

    batches = await _line_batches(db, business_id, [ln.id for ln in active_lines])
    barcodes = await _primary_barcodes(db, business_id, [ln.product_id for ln in active_lines])
    invoice_lines: list[InvoiceLineItem] = []
    for line in active_lines:
        unit_symbol: str | None = None
        variation_name: str | None = None
        allows_decimal = False
        if line.variation is not None:
            variation_name = visible_variation_name(line.variation.name)
            if line.variation.unit is not None:
                unit_symbol = line.variation.unit.symbol
                allows_decimal = bool(line.variation.unit.allows_decimal)
        if unit_symbol is None and line.product.base_unit is not None:
            unit_symbol = line.product.base_unit.symbol
            allows_decimal = bool(line.product.base_unit.allows_decimal)
        sku = line.product.sku
        if line.variation is not None and line.variation.sku:
            sku = line.variation.sku

        line_total = _round2(line.net_amount + line.tax_amount)
        invoice_lines.append(
            InvoiceLineItem(
                line_order=line.line_order,
                product_name=line.product.name,
                product_name_secondary=line.product.name_secondary,
                variation_name=variation_name,
                sku=sku,
                barcode=barcodes.get(line.product_id),
                unit_allows_decimal=allows_decimal,
                batches=batches.get(line.id, []),
                qty=line.qty,
                unit=unit_symbol,
                unit_price=line.unit_price,
                discount_pct=line.discount_pct,
                discount_amount=line.discount_amount,
                tax_rate=line.tax_rate,
                net_amount=line.net_amount,
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

    cfg = await load_tax_settings(db, business_id)
    summary = _build_summary(sale, active_lines, active_payments, cfg)
    cashier_name = await _get_cashier_name(db, sale.created_by)
    register_name = await _register_name(db, business_id, sale.register_shift_id)
    previous_balance, balance_after = await _customer_balances(db, business_id, sale)

    return InvoiceData(
        sale_id=sale.id,
        sale_number=sale.sale_number,
        client_receipt_no=sale.client_receipt_no,
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
        register_name=register_name,
        customer_previous_balance=previous_balance,
        customer_balance_after=balance_after,
        synced=True,
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
        client_receipt_no=invoice.client_receipt_no,
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
