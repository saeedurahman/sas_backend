"""Sale pricing calculations — all monetary math lives here.

Single source of truth for online sales, offline-sync replay, restaurant tabs,
returns, price previews and the settings tax preview. Decimal everywhere.

Two price modes (``inclusive`` flag):

* exclusive: net = qty*price - discount; tax = net*rate; total = net + tax
* inclusive: gross = qty*price - discount (the shelf price already contains
  tax); tax = gross*rate/(100+rate) rounded half-up to 2 dp; net = gross - tax,
  so ``net + tax == gross`` exactly.

Rounding to whole rupees (``none | nearest_1 | nearest_5 | nearest_10``) is only
ever applied to the invoice grand total, half-up; the signed difference is
``rounding_amount``.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from app.models.enums import SaleStatusEnum

ROUNDING_MODES: tuple[str, ...] = ("none", "nearest_1", "nearest_5", "nearest_10")
_ROUNDING_STEPS: dict[str, Decimal] = {
    "nearest_1": Decimal("1"),
    "nearest_5": Decimal("5"),
    "nearest_10": Decimal("10"),
}

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def normalize_rounding_mode(mode: str | None) -> str:
    return mode if mode in ROUNDING_MODES else "none"


def calculate_line_total(
    qty: Decimal,
    unit_price: Decimal,
    discount_pct: Decimal,
    discount_amount: Decimal,
    tax_rate: Decimal,
    *,
    inclusive: bool = False,
) -> dict[str, Decimal]:
    """Price one line.

    ``line_total`` is the line gross (net + tax) in both modes. ``taxable_amount``
    is kept as an alias of ``net_amount`` for older callers.
    """
    qty, unit_price = _dec(qty), _dec(unit_price)
    discount_pct, discount_amount = _dec(discount_pct), _dec(discount_amount)
    tax_rate = _dec(tax_rate)

    line_subtotal = _round2(qty * unit_price)
    pct_discount = _round2(line_subtotal * discount_pct / _HUNDRED)
    effective_discount = _round2(max(discount_amount, pct_discount))
    # A discount can never push a line below zero.
    effective_discount = min(effective_discount, line_subtotal)
    after_discount = _round2(line_subtotal - effective_discount)

    if inclusive:
        # after_discount is the tax-inclusive amount the customer pays for the line.
        tax_amount = (
            _round2(after_discount * tax_rate / (_HUNDRED + tax_rate))
            if tax_rate > _ZERO
            else _ZERO
        )
        net_amount = after_discount - tax_amount
        line_total = after_discount
    else:
        net_amount = after_discount
        tax_amount = _round2(net_amount * tax_rate / _HUNDRED)
        line_total = net_amount + tax_amount

    return {
        "line_subtotal": line_subtotal,
        "effective_discount": effective_discount,
        "taxable_amount": net_amount,
        "net_amount": net_amount,
        "tax_amount": tax_amount,
        "line_total": line_total,
    }


def distribute_cart_discount(
    line_amounts: list[Decimal], cart_discount: Decimal
) -> list[Decimal]:
    """Spread a fixed cart-level discount over lines in proportion to their amounts.

    ``line_amounts`` are what each line is still worth after its own discount.
    Every share is rounded to the cent, the last line takes the remainder and no
    share is ever negative or larger than its line. The shares always add up to
    ``min(cart_discount, sum(line_amounts))``.
    """
    n = len(line_amounts)
    if n == 0:
        return []
    amounts = [max(_dec(a), _ZERO) for a in line_amounts]
    total = sum(amounts, _ZERO)
    target = min(max(_round2(_dec(cart_discount)), _ZERO), total)
    if target <= _ZERO or total <= _ZERO:
        return [_ZERO] * n

    shares = [_ZERO] * n
    for i in range(n - 1):
        shares[i] = min(_round2(target * amounts[i] / total), amounts[i])
    remainder = target - sum(shares[:-1], _ZERO)
    shares[-1] = min(max(remainder, _ZERO), amounts[-1])
    # Whatever the last line could not absorb goes back to lines with headroom.
    leftover = target - sum(shares, _ZERO)
    for i in range(n):
        if leftover <= _ZERO:
            break
        take = min(amounts[i] - shares[i], leftover)
        if take > _ZERO:
            shares[i] += take
            leftover -= take
    return shares


def round_total(exact_total: Decimal, mode: str | None) -> tuple[Decimal, Decimal]:
    """(rounded_total, rounding_amount) for the grand total; half-up to the step."""
    exact_total = _round2(_dec(exact_total))
    step = _ROUNDING_STEPS.get(normalize_rounding_mode(mode))
    if step is None:
        return exact_total, _ZERO
    rounded = (exact_total / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    rounded = rounded.quantize(Decimal("0.01"))
    return rounded, rounded - exact_total


def calculate_sale_totals(
    lines: Iterable[dict], *, rounding_mode: str | None = "none"
) -> dict[str, Decimal]:
    subtotal = _ZERO
    total_discount = _ZERO
    net_amount = _ZERO
    total_tax = _ZERO

    for line in lines:
        subtotal += line["line_subtotal"]
        total_discount += line["effective_discount"]
        net_amount += line.get("net_amount", line.get("taxable_amount"))
        total_tax += line["tax_amount"]

    subtotal = _round2(subtotal)
    total_discount = _round2(total_discount)
    net_amount = _round2(net_amount)
    total_tax = _round2(total_tax)
    exact_total = _round2(net_amount + total_tax)
    total_amount, rounding_amount = round_total(exact_total, rounding_mode)

    return {
        "subtotal": subtotal,
        "total_discount": total_discount,
        "net_amount": net_amount,
        "total_tax": total_tax,
        "exact_total": exact_total,
        "rounding_amount": rounding_amount,
        "total_amount": total_amount,
    }


def price_cart(
    lines: list[dict],
    *,
    inclusive: bool = False,
    rounding_mode: str | None = "none",
    cart_discount: Decimal = _ZERO,
) -> tuple[list[dict[str, Decimal]], dict[str, Decimal]]:
    """Price a whole cart.

    ``lines``: dicts with qty, unit_price, tax_rate and optional discount_pct /
    discount_amount (and nothing else is read). Returns (per-line amounts, totals).
    A cart-level fixed discount is folded into the lines' discounts.
    """
    base = [
        calculate_line_total(
            ln["qty"],
            ln["unit_price"],
            ln.get("discount_pct", _ZERO),
            ln.get("discount_amount", _ZERO),
            ln["tax_rate"],
            inclusive=inclusive,
        )
        for ln in lines
    ]
    if _dec(cart_discount) > _ZERO:
        shares = distribute_cart_discount(
            [b["line_subtotal"] - b["effective_discount"] for b in base], cart_discount
        )
        base = [
            calculate_line_total(
                ln["qty"],
                ln["unit_price"],
                _ZERO,
                b["effective_discount"] + share,
                ln["tax_rate"],
                inclusive=inclusive,
            )
            for ln, b, share in zip(lines, base, shares, strict=True)
        ]
    return base, calculate_sale_totals(base, rounding_mode=rounding_mode)


def determine_sale_status(
    total_amount: Decimal,
    total_paid: Decimal,
) -> SaleStatusEnum:
    if total_amount == 0:
        return SaleStatusEnum.completed
    if total_paid == 0:
        return SaleStatusEnum.draft
    if total_paid < total_amount:
        return SaleStatusEnum.partially_paid
    return SaleStatusEnum.completed
