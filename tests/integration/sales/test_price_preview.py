"""Sale price preview endpoint tests (migrated from test_sale_price_preview)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests.helpers.db import db_scalar
from tests.helpers.records import assert_ok
from tests.helpers.sales import sale_line

pytestmark = pytest.mark.integration


def _db_counts(business_id: str) -> dict[str, int]:
    queries = {
        "sales": "SELECT COUNT(*) FROM sales WHERE business_id = :bid",
        "sale_lines": "SELECT COUNT(*) FROM sale_lines WHERE business_id = :bid",
        "stock_movements": (
            "SELECT COUNT(*) FROM stock_movements WHERE business_id = :bid"
        ),
        "purchase_lines": (
            "SELECT COUNT(*) FROM purchase_lines WHERE business_id = :bid"
        ),
    }
    return {
        key: int(db_scalar(sql, {"bid": business_id}) or 0)
        for key, sql in queries.items()
    }


def _setup_tenant(client: httpx.Client, api_base: str, suffix: int) -> dict:
    password = "TestPass1"
    owner_phone = f"610{suffix:07d}"
    reg = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Preview {suffix}",
            "business_type_code": "retail",
            "owner_name": "Preview Owner",
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

    unit_id = client.post(
        f"{api_base}/units",
        headers=headers,
        json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
    ).json()["id"]

    def make_product(name: str) -> str:
        resp = client.post(
            f"{api_base}/products",
            headers=headers,
            json={
                "name": name,
                "base_unit_id": unit_id,
                "product_type": "standard",
                "tracking_type": "none",
            },
        )
        if resp.status_code != 201:
            raise RuntimeError(f"product failed: {resp.status_code}")
        return resp.json()["id"]

    product_a = make_product(f"Product A {suffix}")
    product_b = make_product(f"Product B {suffix}")

    for product_id in (product_a, product_b):
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
                        "cost_per_unit": "10.00",
                    }
                ],
            },
        )

    return {
        "headers": headers,
        "branch_id": branch_id,
        "business_id": business_id,
        "product_a": product_a,
        "product_b": product_b,
    }


def _create_scheme(
    client: httpx.Client,
    api_base: str,
    headers: dict,
    name: str,
    discount_type: str,
    discount_value: str,
) -> dict:
    resp = client.post(
        f"{api_base}/discounts",
        headers=headers,
        json={
            "name": name,
            "discount_type": discount_type,
            "discount_value": discount_value,
        },
    )
    if resp.status_code != 201:
        raise RuntimeError(f"scheme failed: {resp.status_code} {resp.text}")
    return resp.json()


def _sale_body(
    ctx: dict,
    lines: list[dict],
    discount_scheme_id: str | None = None,
) -> dict:
    body: dict = {"branch_id": ctx["branch_id"], "lines": lines}
    if discount_scheme_id is not None:
        body["discount_scheme_id"] = discount_scheme_id
    return body


def _preview(client: httpx.Client, api_base: str, ctx: dict, body: dict) -> httpx.Response:
    return client.post(
        f"{api_base}/sales/price-preview",
        headers=ctx["headers"],
        json=body,
    )


def _create_sale(
    client: httpx.Client,
    api_base: str,
    ctx: dict,
    body: dict,
    payment_amount: str,
) -> httpx.Response:
    return client.post(
        f"{api_base}/sales",
        headers=ctx["headers"],
        json={
            **body,
            "payments": [{"payment_method": "cash", "amount": payment_amount}],
        },
    )


def _line_by_product(body: dict, product_id: str) -> dict:
    for line in body["lines"]:
        if line["product_id"] == product_id:
            return line
    raise KeyError(product_id)


def test_sale_price_preview(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix)

    pct_scheme = _create_scheme(
        http_client, api_base, ctx["headers"], "Ten Percent", "percentage", "10"
    )
    fixed_scheme = _create_scheme(
        http_client, api_base, ctx["headers"], "Fifty Off", "fixed_amount", "50"
    )

    pct_body = _sale_body(
        ctx,
        [
            sale_line(ctx["product_a"], "2", "100.00", line_order=0),
            sale_line(ctx["product_b"], "1", "50.00", line_order=1),
        ],
        discount_scheme_id=pct_scheme["id"],
    )
    prev = _preview(http_client, api_base, ctx, pct_body)
    sale = _create_sale(http_client, api_base, ctx, pct_body, "225.00")
    if prev.status_code == 200 and sale.status_code == 201:
        prev_json = prev.json()
        sale_json = sale.json()
        ok = (
            Decimal(prev_json["total_amount"]) == Decimal("225.00")
            and Decimal(sale_json["lines"][0]["discount_amount"])
            == Decimal(prev_json["lines"][0]["discount_amount"])
            and Decimal(sale_json["lines"][1]["discount_amount"])
            == Decimal(prev_json["lines"][1]["discount_amount"])
            and Decimal(prev_json["total_amount"])
            == Decimal(prev_json["subtotal"])
            - Decimal(prev_json["total_discount"])
            + Decimal(prev_json["total_tax"])
        )
        assert_ok(
            "percentage preview matches create_sale",
            ok,
            f"preview total={prev_json['total_amount']}, "
            f"A disc={prev_json['lines'][0]['discount_amount']}",
        )
    else:
        assert_ok(
            "percentage preview matches create_sale",
            False,
            f"preview={prev.status_code}, create={sale.status_code}",
        )

    fixed_body = _sale_body(
        ctx,
        [
            sale_line(ctx["product_a"], "1", "120.00", line_order=0),
            sale_line(ctx["product_b"], "1", "80.00", line_order=1),
        ],
        discount_scheme_id=fixed_scheme["id"],
    )
    prev = _preview(http_client, api_base, ctx, fixed_body)
    sale = _create_sale(http_client, api_base, ctx, fixed_body, "150.00")
    if prev.status_code == 200 and sale.status_code == 201:
        prev_json = prev.json()
        sale_json = sale.json()
        prev_sum = sum(Decimal(line["discount_amount"]) for line in prev_json["lines"])
        sale_sum = sum(Decimal(line["discount_amount"]) for line in sale_json["lines"])
        ok = (
            prev_sum == Decimal("50.00")
            and sale_sum == Decimal("50.00")
            and Decimal(prev_json["lines"][0]["discount_amount"])
            == Decimal(sale_json["lines"][0]["discount_amount"])
            and Decimal(prev_json["lines"][1]["discount_amount"])
            == Decimal(sale_json["lines"][1]["discount_amount"])
            and Decimal(prev_json["total_amount"]) == Decimal("150.00")
        )
        assert_ok(
            "fixed pro-rata preview matches create_sale",
            ok,
            f"preview disc sum={prev_sum}, sale disc sum={sale_sum}",
        )
    else:
        assert_ok(
            "fixed pro-rata preview matches create_sale",
            False,
            f"preview={prev.status_code}, create={sale.status_code}",
        )

    mixed_body = _sale_body(
        ctx,
        [
            sale_line(
                ctx["product_a"],
                "1",
                "100.00",
                discount_amount="15.00",
                line_order=0,
            ),
            sale_line(ctx["product_b"], "1", "100.00", line_order=1),
        ],
        discount_scheme_id=pct_scheme["id"],
    )
    prev = _preview(http_client, api_base, ctx, mixed_body)
    sale = _create_sale(http_client, api_base, ctx, mixed_body, "175.00")
    if prev.status_code == 200 and sale.status_code == 201:
        prev_json = prev.json()
        sale_json = sale.json()
        pa = _line_by_product(prev_json, ctx["product_a"])
        pb = _line_by_product(prev_json, ctx["product_b"])
        sa = _line_by_product(sale_json, ctx["product_a"])
        sb = _line_by_product(sale_json, ctx["product_b"])
        ok = (
            Decimal(pa["discount_amount"]) == Decimal("15.00")
            and Decimal(pb["discount_amount"]) == Decimal("10.00")
            and Decimal(sa["discount_amount"]) == Decimal(pa["discount_amount"])
            and Decimal(sb["discount_amount"]) == Decimal(pb["discount_amount"])
            and Decimal(prev_json["total_amount"]) == Decimal("175.00")
        )
        assert_ok(
            "mixed manual+scheme preview matches create_sale",
            ok,
            f"manual A={pa['discount_amount']}, scheme B={pb['discount_amount']}",
        )
    else:
        assert_ok(
            "mixed manual+scheme preview matches create_sale",
            False,
            f"preview={prev.status_code}, create={sale.status_code}",
        )

    expired = http_client.post(
        f"{api_base}/discounts",
        headers=ctx["headers"],
        json={
            "name": "Expired",
            "discount_type": "percentage",
            "discount_value": "10",
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_to": "2020-12-31T23:59:59Z",
        },
    ).json()
    bad_body = _sale_body(
        ctx,
        [sale_line(ctx["product_a"], "1", "50.00")],
        discount_scheme_id=expired["id"],
    )
    prev = _preview(http_client, api_base, ctx, bad_body)
    assert_ok(
        "expired scheme preview returns 400",
        prev.status_code == 400 and "expired" in prev.text.lower(),
        f"status={prev.status_code}",
    )

    counts_before = _db_counts(ctx["business_id"])
    noop_body = _sale_body(
        ctx,
        [sale_line(ctx["product_a"], "1", "25.00")],
        discount_scheme_id=pct_scheme["id"],
    )
    all_ok = True
    for _ in range(3):
        resp = _preview(http_client, api_base, ctx, noop_body)
        if resp.status_code != 200:
            all_ok = False
            break
    counts_after = _db_counts(ctx["business_id"])
    unchanged = counts_before == counts_after
    assert_ok(
        "preview creates no sales/lines/movements/purchase_lines",
        all_ok and unchanged,
        f"before={counts_before}, after={counts_after}",
    )
