"""Sale pricing calculations — all monetary math lives here."""

from decimal import ROUND_HALF_UP, Decimal

from app.models.enums import SaleStatusEnum


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_line_total(
    qty: Decimal,
    unit_price: Decimal,
    discount_pct: Decimal,
    discount_amount: Decimal,
    tax_rate: Decimal,
) -> dict[str, Decimal]:
    line_subtotal = _round2(qty * unit_price)
    pct_discount = _round2(line_subtotal * discount_pct / Decimal("100"))
    effective_discount = max(discount_amount, pct_discount)
    effective_discount = _round2(effective_discount)
    taxable_amount = _round2(line_subtotal - effective_discount)
    tax_amount = _round2(taxable_amount * tax_rate / Decimal("100"))
    line_total = _round2(taxable_amount + tax_amount)
    return {
        "line_subtotal": line_subtotal,
        "effective_discount": effective_discount,
        "taxable_amount": taxable_amount,
        "tax_amount": tax_amount,
        "line_total": line_total,
    }


def calculate_sale_totals(lines: list[dict]) -> dict[str, Decimal]:
    subtotal = Decimal("0")
    total_discount = Decimal("0")
    total_tax = Decimal("0")

    for line in lines:
        subtotal += line["line_subtotal"]
        total_discount += line["effective_discount"]
        total_tax += line["tax_amount"]

    subtotal = _round2(subtotal)
    total_discount = _round2(total_discount)
    total_tax = _round2(total_tax)
    total_amount = _round2(subtotal - total_discount + total_tax)

    return {
        "subtotal": subtotal,
        "total_discount": total_discount,
        "total_tax": total_tax,
        "total_amount": total_amount,
    }


def determine_sale_status(
    total_amount: Decimal,
    total_paid: Decimal,
) -> SaleStatusEnum:
    if total_paid == 0:
        return SaleStatusEnum.draft
    if total_paid < total_amount:
        return SaleStatusEnum.partially_paid
    return SaleStatusEnum.completed
