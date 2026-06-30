"""Discount scheme eligibility and per-line discount allocation."""

from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from fastapi import HTTPException, status

from app.models.enums import DiscountTypeEnum
from app.models.sales import DiscountScheme
from app.services.invoice_service import _round2

_ZERO = Decimal("0")


class SaleLineInput(Protocol):
    qty: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    product_id: UUID


def has_manual_discount(line: SaleLineInput) -> bool:
    return line.discount_pct > _ZERO or line.discount_amount > _ZERO


def line_subtotal(qty: Decimal, unit_price: Decimal) -> Decimal:
    return _round2(qty * unit_price)


def _as_uuid_set(values: Any) -> set[UUID]:
    if not values:
        return set()
    result: set[UUID] = set()
    for value in values:
        result.add(value if isinstance(value, UUID) else UUID(str(value)))
    return result


def parse_applies_to(applies_to_json: dict[str, Any] | None) -> dict[str, Any]:
    if not applies_to_json:
        return {
            "scope": "all",
            "product_ids": set(),
            "category_ids": set(),
        }
    scope = applies_to_json.get("scope") or "all"
    return {
        "scope": scope,
        "product_ids": _as_uuid_set(applies_to_json.get("product_ids")),
        "category_ids": _as_uuid_set(applies_to_json.get("category_ids")),
    }


def is_line_scope_eligible(
    product_id: UUID,
    category_id: UUID | None,
    applies: dict[str, Any],
) -> bool:
    scope = applies["scope"]
    if scope == "all":
        return True
    if scope == "product":
        return product_id in applies["product_ids"]
    if scope == "category":
        return category_id is not None and category_id in applies["category_ids"]
    return False


def merge_line_discount_inputs(
    line: SaleLineInput,
    scheme_inputs: dict[str, Decimal] | None,
) -> tuple[Decimal, Decimal]:
    if has_manual_discount(line):
        return line.discount_pct, line.discount_amount
    if scheme_inputs is not None:
        return scheme_inputs["discount_pct"], scheme_inputs["discount_amount"]
    return line.discount_pct, line.discount_amount


def apply_discount_scheme(
    scheme: DiscountScheme,
    lines: list[SaleLineInput],
    product_category_by_id: dict[UUID, UUID | None],
) -> list[dict[str, Decimal]]:
    """Return per-line scheme discount inputs (discount_pct, discount_amount).

    Manual discounts are not applied here; callers merge via merge_line_discount_inputs.
    Raises HTTPException when the scheme cannot apply to this sale.
    """
    applies = parse_applies_to(scheme.applies_to_json)
    subtotals = [
        line_subtotal(line.qty, line.unit_price) for line in lines
    ]

    eligible_indices: list[int] = []
    for index, line in enumerate(lines):
        if has_manual_discount(line):
            continue
        category_id = product_category_by_id.get(line.product_id)
        if is_line_scope_eligible(line.product_id, category_id, applies):
            eligible_indices.append(index)

    if not eligible_indices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No sale lines eligible for discount scheme",
        )

    eligible_subtotal = _round2(
        sum(subtotals[index] for index in eligible_indices)
    )

    if (
        scheme.min_purchase_amount is not None
        and eligible_subtotal < scheme.min_purchase_amount
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Minimum purchase amount not met for discount scheme",
        )

    results: list[dict[str, Decimal]] = [
        {"discount_pct": _ZERO, "discount_amount": _ZERO}
        for _ in lines
    ]

    if scheme.discount_type == DiscountTypeEnum.percentage.value:
        for index in eligible_indices:
            results[index] = {
                "discount_pct": scheme.discount_value,
                "discount_amount": _ZERO,
            }
        return results

    if scheme.discount_type == DiscountTypeEnum.fixed_amount.value:
        total_discount = min(scheme.discount_value, eligible_subtotal)
        allocated = _ZERO
        eligible_count = len(eligible_indices)
        for position, index in enumerate(eligible_indices):
            line_total = subtotals[index]
            if position == eligible_count - 1:
                line_discount = _round2(total_discount - allocated)
            else:
                share = total_discount * line_total / eligible_subtotal
                line_discount = _round2(share)
                allocated += line_discount
            line_discount = min(line_discount, line_total)
            results[index] = {
                "discount_pct": _ZERO,
                "discount_amount": line_discount,
            }
        return results

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported discount scheme type: {scheme.discount_type}",
    )
