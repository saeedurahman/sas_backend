"""Sale request payload helpers."""

from __future__ import annotations

from typing import Any


def sale_line(
    product_id: str,
    qty: str,
    unit_price: str,
    *,
    discount_pct: str = "0",
    discount_amount: str = "0",
    line_order: int = 0,
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "qty": qty,
        "unit_price": unit_price,
        "discount_pct": discount_pct,
        "discount_amount": discount_amount,
        "line_order": line_order,
    }


def create_sale_payload(
    *,
    branch_id: str,
    product_id: str,
    qty: str = "1",
    unit_price: str = "100.00",
    payments: list[dict[str, Any]] | None = None,
    customer_id: str | None = None,
    register_shift_id: str | None = None,
) -> dict[str, Any]:
    if payments is None:
        payments = [{"payment_method": "cash", "amount": unit_price}]
    body: dict[str, Any] = {
        "branch_id": branch_id,
        "lines": [sale_line(product_id, qty, unit_price)],
        "payments": payments,
    }
    if customer_id is not None:
        body["customer_id"] = customer_id
    if register_shift_id is not None:
        body["register_shift_id"] = register_shift_id
    return body
