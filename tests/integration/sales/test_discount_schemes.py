"""Discount scheme auto-apply integration tests (migrated from script)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest

from app.services.discount_engine import apply_discount_scheme
from app.services.invoice_service import _round2
from app.services.pricing_engine import calculate_line_total
from tests.helpers.db import db_scalar
from tests.helpers.records import assert_ok
from tests.helpers.sales import sale_line

pytestmark = pytest.mark.integration


class _Line:
    def __init__(
        self,
        product_id: uuid.UUID,
        qty: Decimal,
        unit_price: Decimal,
        discount_pct: Decimal = Decimal("0"),
        discount_amount: Decimal = Decimal("0"),
    ) -> None:
        self.product_id = product_id
        self.qty = qty
        self.unit_price = unit_price
        self.discount_pct = discount_pct
        self.discount_amount = discount_amount


class _Scheme:
    def __init__(
        self,
        discount_type: str,
        discount_value: Decimal,
        min_purchase_amount: Decimal | None = None,
        applies_to_json: dict | None = None,
    ) -> None:
        self.discount_type = discount_type
        self.discount_value = discount_value
        self.min_purchase_amount = min_purchase_amount
        self.applies_to_json = applies_to_json or {}


def test_pro_rata_unit() -> None:
    pid = uuid.uuid4()
    lines = [
        _Line(pid, Decimal("1"), Decimal("120.00")),
        _Line(pid, Decimal("1"), Decimal("80.00")),
    ]
    scheme = _Scheme("fixed_amount", Decimal("50.00"))
    result = apply_discount_scheme(scheme, lines, {pid: None})
    amounts = [row["discount_amount"] for row in result]
    total = sum(amounts, Decimal("0"))
    ok = total == Decimal("50.00") and amounts == [
        Decimal("30.00"),
        Decimal("20.00"),
    ]
    assert_ok(
        "unit fixed pro-rata sums to scheme value",
        ok,
        f"amounts={amounts}, total={total}",
    )


def _setup_tenant(client: httpx.Client, api_base: str, suffix: int) -> dict:
    password = "TestPass1"
    owner_phone = f"510{suffix:07d}"
    reg = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Discount {suffix}",
            "business_type_code": "retail",
            "owner_name": "Discount Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    if reg.status_code != 201:
        raise RuntimeError(f"register failed: {reg.status_code} {reg.text}")
    body = reg.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    branch_id = body["user"]["branch_id"]
    business_id = body["user"]["business_id"]

    unit = client.post(
        f"{api_base}/units",
        headers=headers,
        json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
    )
    unit_id = unit.json()["id"]

    def make_product(name: str, category_id: str | None = None) -> str:
        payload = {
            "name": name,
            "base_unit_id": unit_id,
            "product_type": "standard",
            "tracking_type": "none",
        }
        if category_id is not None:
            payload["category_id"] = category_id
        resp = client.post(f"{api_base}/products", headers=headers, json=payload)
        if resp.status_code != 201:
            raise RuntimeError(f"product create failed: {resp.status_code}")
        return resp.json()["id"]

    product_a = make_product(f"Product A {suffix}")
    product_b = make_product(f"Product B {suffix}")

    category_resp = client.post(
        f"{api_base}/categories",
        headers=headers,
        json={"name": f"Promo Cat {suffix}"},
    )
    category_id = category_resp.json()["id"]
    product_cat = make_product(f"Product Cat {suffix}", category_id=category_id)
    product_other = make_product(f"Product Other {suffix}")

    client.put(
        f"{api_base}/products/{product_cat}",
        headers=headers,
        json={"category_id": category_id},
    )

    supplier = client.post(
        f"{api_base}/suppliers",
        headers=headers,
        json={"name": f"Supplier {suffix}"},
    )
    supplier_id = supplier.json()["id"]

    for product_id, cost in (
        (product_a, "25.00"),
        (product_b, "30.00"),
        (product_cat, "40.00"),
        (product_other, "20.00"),
    ):
        client.post(
            f"{api_base}/adjustments",
            headers=headers,
            json={
                "branch_id": branch_id,
                "reason": "opening_balance",
                "lines": [
                    {
                        "product_id": product_id,
                        "qty_delta": "100",
                        "cost_per_unit": cost,
                    }
                ],
            },
        )

    return {
        "headers": headers,
        "branch_id": branch_id,
        "business_id": business_id,
        "unit_id": unit_id,
        "product_a": product_a,
        "product_b": product_b,
        "product_cat": product_cat,
        "product_other": product_other,
        "category_id": category_id,
        "supplier_id": supplier_id,
    }


def _create_scheme(
    client: httpx.Client,
    api_base: str,
    headers: dict,
    name: str,
    discount_type: str,
    discount_value: str,
    **kwargs,
) -> dict:
    body = {
        "name": name,
        "discount_type": discount_type,
        "discount_value": discount_value,
        **kwargs,
    }
    resp = client.post(f"{api_base}/discounts", headers=headers, json=body)
    if resp.status_code != 201:
        raise RuntimeError(f"scheme create failed: {resp.status_code} {resp.text}")
    return resp.json()


def _create_sale(
    client: httpx.Client,
    api_base: str,
    ctx: dict,
    lines: list[dict],
    discount_scheme_id: str | None = None,
    payments: list[dict] | None = None,
) -> httpx.Response:
    body: dict = {
        "branch_id": ctx["branch_id"],
        "lines": lines,
        "payments": payments or [],
    }
    if discount_scheme_id is not None:
        body["discount_scheme_id"] = discount_scheme_id
    return client.post(f"{api_base}/sales", headers=ctx["headers"], json=body)


def _line_by_product(sale_body: dict, product_id: str) -> dict:
    for line in sale_body["lines"]:
        if line["product_id"] == product_id:
            return line
    raise KeyError(product_id)


def _expected_manual_totals(lines: list[dict]) -> dict[str, Decimal]:
    computed: list[dict] = []
    for line in lines:
        computed.append(
            calculate_line_total(
                qty=Decimal(line["qty"]),
                unit_price=Decimal(line["unit_price"]),
                discount_pct=Decimal(line.get("discount_pct", "0")),
                discount_amount=Decimal(line.get("discount_amount", "0")),
                tax_rate=Decimal(line.get("tax_rate", "0")),
            )
        )
    subtotal = sum(row["line_subtotal"] for row in computed)
    total_discount = sum(row["effective_discount"] for row in computed)
    total_tax = sum(row["tax_amount"] for row in computed)
    total_amount = _round2(subtotal - total_discount + total_tax)
    return {
        "subtotal": _round2(subtotal),
        "total_discount": _round2(total_discount),
        "total_amount": total_amount,
    }


def _add_po_stock(
    client: httpx.Client,
    api_base: str,
    ctx: dict,
    product_id: str,
    qty: str,
    cost: str,
) -> None:
    po = client.post(
        f"{api_base}/purchases/orders",
        headers=ctx["headers"],
        json={
            "supplier_id": ctx["supplier_id"],
            "branch_id": ctx["branch_id"],
            "lines": [
                {
                    "product_id": product_id,
                    "ordered_qty": qty,
                    "cost_per_unit": cost,
                }
            ],
        },
    )
    if po.status_code != 201:
        raise RuntimeError(f"PO create failed: {po.status_code} {po.text}")
    po_body = po.json()
    po_line_id = po_body["lines"][0]["id"]
    receipt = client.post(
        f"{api_base}/purchases/receipts",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "supplier_id": ctx["supplier_id"],
            "purchase_order_id": po_body["id"],
            "lines": [
                {
                    "product_id": product_id,
                    "purchase_line_id": po_line_id,
                    "qty_received": qty,
                    "cost_per_unit": cost,
                }
            ],
        },
    )
    if receipt.status_code != 201:
        raise RuntimeError(f"receipt failed: {receipt.status_code} {receipt.text}")


def _sale_grand_total(body: dict) -> Decimal:
    total = Decimal("0")
    for line in body["lines"]:
        subtotal = Decimal(line["qty"]) * Decimal(line["unit_price"])
        after_discount = subtotal - Decimal(line["discount_amount"])
        total += after_discount + Decimal(line["tax_amount"])
    return _round2(total)


def test_discount_scheme_apply(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix)

    pct_scheme = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "Ten Percent",
        "percentage",
        "10",
    )
    lines = [
        sale_line(ctx["product_a"], "2", "100.00", line_order=0),
        sale_line(ctx["product_b"], "1", "50.00", line_order=1),
    ]
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        lines,
        discount_scheme_id=pct_scheme["id"],
        payments=[{"payment_method": "cash", "amount": "225.00"}],
    )
    if resp.status_code == 201:
        body = resp.json()
        la = _line_by_product(body, ctx["product_a"])
        lb = _line_by_product(body, ctx["product_b"])
        ok = (
            Decimal(la["discount_amount"]) == Decimal("20.00")
            and Decimal(lb["discount_amount"]) == Decimal("5.00")
            and _sale_grand_total(body) == Decimal("225.00")
        )
        assert_ok(
            "percentage scheme on all eligible lines",
            ok,
            f"A disc={la['discount_amount']}, B disc={lb['discount_amount']}, total={_sale_grand_total(body)}",
        )
    else:
        assert_ok("percentage scheme on all eligible lines", False, resp.text[:200])

    fixed_scheme = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "Fifty Off",
        "fixed_amount",
        "50",
    )
    lines = [
        sale_line(ctx["product_a"], "1", "120.00", line_order=0),
        sale_line(ctx["product_b"], "1", "80.00", line_order=1),
    ]
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        lines,
        discount_scheme_id=fixed_scheme["id"],
        payments=[{"payment_method": "cash", "amount": "150.00"}],
    )
    if resp.status_code == 201:
        body = resp.json()
        la = _line_by_product(body, ctx["product_a"])
        lb = _line_by_product(body, ctx["product_b"])
        disc_sum = Decimal(la["discount_amount"]) + Decimal(lb["discount_amount"])
        ok = (
            Decimal(la["discount_amount"]) == Decimal("30.00")
            and Decimal(lb["discount_amount"]) == Decimal("20.00")
            and disc_sum == Decimal("50.00")
            and _sale_grand_total(body) == Decimal("150.00")
        )
        assert_ok(
            "fixed scheme pro-rata with exact rounding",
            ok,
            f"A={la['discount_amount']}, B={lb['discount_amount']}, sum={disc_sum}",
        )
    else:
        assert_ok("fixed scheme pro-rata with exact rounding", False, resp.text[:200])

    mixed_lines = [
        sale_line(
            ctx["product_a"],
            "1",
            "100.00",
            discount_amount="15.00",
            line_order=0,
        ),
        sale_line(ctx["product_b"], "1", "100.00", line_order=1),
    ]
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        mixed_lines,
        discount_scheme_id=pct_scheme["id"],
        payments=[{"payment_method": "cash", "amount": "175.00"}],
    )
    if resp.status_code == 201:
        body = resp.json()
        la = _line_by_product(body, ctx["product_a"])
        lb = _line_by_product(body, ctx["product_b"])
        ok = (
            Decimal(la["discount_amount"]) == Decimal("15.00")
            and Decimal(lb["discount_amount"]) == Decimal("10.00")
            and _sale_grand_total(body) == Decimal("175.00")
        )
        assert_ok(
            "mixed sale manual wins on one line scheme on other",
            ok,
            f"manual A={la['discount_amount']}, scheme B={lb['discount_amount']}",
        )
    else:
        assert_ok(
            "mixed sale manual wins on one line scheme on other",
            False,
            resp.text[:200],
        )

    product_scope = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "Product A only",
        "percentage",
        "20",
        applies_to_json={
            "scope": "product",
            "product_ids": [ctx["product_a"]],
        },
    )
    scope_lines = [
        sale_line(ctx["product_a"], "1", "100.00", line_order=0),
        sale_line(ctx["product_b"], "1", "100.00", line_order=1),
    ]
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        scope_lines,
        discount_scheme_id=product_scope["id"],
        payments=[{"payment_method": "cash", "amount": "180.00"}],
    )
    if resp.status_code == 201:
        body = resp.json()
        la = _line_by_product(body, ctx["product_a"])
        lb = _line_by_product(body, ctx["product_b"])
        ok = (
            Decimal(la["discount_amount"]) == Decimal("20.00")
            and Decimal(lb["discount_amount"]) == Decimal("0.00")
        )
        assert_ok(
            "product scope applies only to matching product_ids",
            ok,
            f"A={la['discount_amount']}, B={lb['discount_amount']}",
        )
    else:
        assert_ok(
            "product scope applies only to matching product_ids",
            False,
            resp.text[:200],
        )

    cat_scheme = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "Category promo",
        "percentage",
        "25",
        applies_to_json={
            "scope": "category",
            "category_ids": [ctx["category_id"]],
        },
    )
    cat_lines = [
        sale_line(ctx["product_cat"], "1", "100.00", line_order=0),
        sale_line(ctx["product_other"], "1", "100.00", line_order=1),
    ]
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        cat_lines,
        discount_scheme_id=cat_scheme["id"],
        payments=[{"payment_method": "cash", "amount": "175.00"}],
    )
    if resp.status_code == 201:
        body = resp.json()
        lc = _line_by_product(body, ctx["product_cat"])
        lo = _line_by_product(body, ctx["product_other"])
        ok = (
            Decimal(lc["discount_amount"]) == Decimal("25.00")
            and Decimal(lo["discount_amount"]) == Decimal("0.00")
        )
        assert_ok(
            "category scope applies only to matching category lines",
            ok,
            f"cat={lc['discount_amount']}, other={lo['discount_amount']}",
        )
    else:
        assert_ok(
            "category scope applies only to matching category lines",
            False,
            resp.text[:200],
        )

    min_scheme = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "High minimum",
        "percentage",
        "10",
        min_purchase_amount="500.00",
    )
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        [sale_line(ctx["product_a"], "1", "100.00")],
        discount_scheme_id=min_scheme["id"],
    )
    assert_ok(
        "min_purchase_amount not met returns 400",
        resp.status_code == 400 and "Minimum purchase amount" in resp.text,
        f"status={resp.status_code}",
    )

    big_fixed = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "Big fixed",
        "fixed_amount",
        "500",
    )
    cap_lines = [sale_line(ctx["product_a"], "1", "80.00")]
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        cap_lines,
        discount_scheme_id=big_fixed["id"],
        payments=[],
    )
    if resp.status_code == 201:
        body = resp.json()
        line = body["lines"][0]
        ok = (
            Decimal(line["discount_amount"]) == Decimal("80.00")
            and _sale_grand_total(body) == Decimal("0.00")
        )
        assert_ok(
            "fixed discount capped at eligible subtotal",
            ok,
            f"discount={line['discount_amount']}, total={_sale_grand_total(body)}",
        )
    else:
        assert_ok("fixed discount capped at eligible subtotal", False, resp.text[:200])

    expired = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "Expired",
        "percentage",
        "10",
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2020-12-31T23:59:59Z",
    )
    before_count = db_scalar(
        "SELECT COUNT(*) FROM sales WHERE business_id = :bid",
        {"bid": ctx["business_id"]},
    )
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        [sale_line(ctx["product_a"], "1", "50.00")],
        discount_scheme_id=expired["id"],
    )
    after_count = db_scalar(
        "SELECT COUNT(*) FROM sales WHERE business_id = :bid",
        {"bid": ctx["business_id"]},
    )
    assert_ok(
        "expired scheme returns 400 and sale not created",
        resp.status_code == 400
        and "expired" in resp.text.lower()
        and before_count == after_count,
        f"status={resp.status_code}, sales before={before_count} after={after_count}",
    )

    inactive = _create_scheme(
        http_client,
        api_base,
        ctx["headers"],
        "Inactive",
        "percentage",
        "10",
    )
    http_client.put(
        f"{api_base}/discounts/{inactive['id']}",
        headers=ctx["headers"],
        json={"is_active": False},
    )
    before_count = db_scalar(
        "SELECT COUNT(*) FROM sales WHERE business_id = :bid",
        {"bid": ctx["business_id"]},
    )
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        [sale_line(ctx["product_a"], "1", "50.00")],
        discount_scheme_id=inactive["id"],
    )
    after_count = db_scalar(
        "SELECT COUNT(*) FROM sales WHERE business_id = :bid",
        {"bid": ctx["business_id"]},
    )
    assert_ok(
        "inactive scheme returns 400",
        resp.status_code == 400 and before_count == after_count,
        f"status={resp.status_code}",
    )

    fifo_product = http_client.post(
        f"{api_base}/products",
        headers=ctx["headers"],
        json={
            "name": f"FIFO Only {uuid.uuid4().hex[:8]}",
            "base_unit_id": ctx["unit_id"],
            "product_type": "standard",
            "tracking_type": "none",
        },
    ).json()["id"]
    fifo_cost = "33.50"
    _add_po_stock(http_client, api_base, ctx, fifo_product, "10", fifo_cost)
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        [sale_line(fifo_product, "1", "100.00")],
        discount_scheme_id=pct_scheme["id"],
        payments=[{"payment_method": "cash", "amount": "90.00"}],
    )
    if resp.status_code == 201:
        body = resp.json()
        line = _line_by_product(body, fifo_product)
        ok = (
            Decimal(line["discount_amount"]) == Decimal("10.00")
            and Decimal(line["cost_per_unit"]) == Decimal(fifo_cost)
        )
        assert_ok(
            "FIFO cost_per_unit unchanged when scheme discount applied",
            ok,
            f"cost={line['cost_per_unit']}, expected={fifo_cost}, disc={line['discount_amount']}",
        )
    else:
        assert_ok(
            "FIFO cost_per_unit unchanged when scheme discount applied",
            False,
            resp.text[:200],
        )

    manual_lines = [
        sale_line(
            ctx["product_b"],
            "2",
            "50.00",
            discount_pct="10",
            line_order=0,
        ),
    ]
    expected = _expected_manual_totals(manual_lines)
    resp = _create_sale(
        http_client,
        api_base,
        ctx,
        manual_lines,
        payments=[
            {
                "payment_method": "cash",
                "amount": str(expected["total_amount"]),
            }
        ],
    )
    if resp.status_code == 201:
        body = resp.json()
        line = body["lines"][0]
        ok = (
            Decimal(line["discount_amount"]) == expected["total_discount"]
            and _sale_grand_total(body) == expected["total_amount"]
        )
        assert_ok(
            "manual-only sale without scheme unchanged",
            ok,
            f"disc={line['discount_amount']}, total={_sale_grand_total(body)}",
        )
    else:
        assert_ok("manual-only sale without scheme unchanged", False, resp.text[:200])
