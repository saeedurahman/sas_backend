"""Sale pricing permission enforcement (migrated from test_sale_pricing_permissions)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.records import assert_ok
from tests.helpers.sales import sale_line
from tests.helpers.tenants import login

pytestmark = pytest.mark.integration


def _setup_tenant(client: httpx.Client, api_base: str, suffix: int) -> dict:
    password = "TestPass1"
    owner_phone = f"720{suffix:07d}"
    manager_phone = f"721{suffix:07d}"
    cashier_phone = f"722{suffix:07d}"

    reg = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Pricing Perm {suffix}",
            "business_type_code": "retail",
            "owner_name": "Pricing Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    if reg.status_code != 201:
        raise RuntimeError(f"register failed: {reg.status_code} {reg.text}")
    reg_body = reg.json()
    owner_headers = {"Authorization": f"Bearer {reg_body['access_token']}"}
    branch_id = reg_body["user"]["branch_id"]

    roles = client.get(f"{api_base}/roles", headers=owner_headers).json()
    manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
    cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")

    for phone, name, role_id in (
        (manager_phone, "Manager", manager_role_id),
        (cashier_phone, "Cashier", cashier_role_id),
    ):
        resp = client.post(
            f"{api_base}/users",
            headers=owner_headers,
            json={
                "full_name": name,
                "phone": phone,
                "password": password,
                "role_ids": [role_id],
            },
        )
        if resp.status_code != 201:
            raise RuntimeError(f"create {name} failed: {resp.status_code} {resp.text}")

    unit_id = client.post(
        f"{api_base}/units",
        headers=owner_headers,
        json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
    ).json()["id"]

    product = client.post(
        f"{api_base}/products",
        headers=owner_headers,
        json={
            "name": f"Catalog Product {suffix}",
            "base_unit_id": unit_id,
            "product_type": "standard",
            "tracking_type": "none",
        },
    )
    if product.status_code != 201:
        raise RuntimeError(f"product failed: {product.status_code}")
    product_id = product.json()["id"]

    client.post(
        f"{api_base}/adjustments",
        headers=owner_headers,
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

    price_list = client.post(
        f"{api_base}/prices/lists",
        headers=owner_headers,
        json={"name": "Retail", "list_type": "retail", "is_default": True},
    )
    if price_list.status_code != 201:
        raise RuntimeError(f"price list failed: {price_list.status_code}")
    price_list_id = price_list.json()["id"]

    set_price = client.post(
        f"{api_base}/prices/lists/{price_list_id}/items",
        headers=owner_headers,
        json={"product_id": product_id, "unit_price": "25.00"},
    )
    if set_price.status_code != 201:
        raise RuntimeError(f"set price failed: {set_price.status_code}")

    scheme = client.post(
        f"{api_base}/discounts",
        headers=owner_headers,
        json={
            "name": "Ten Percent",
            "discount_type": "percentage",
            "discount_value": "10",
        },
    )
    if scheme.status_code != 201:
        raise RuntimeError(f"scheme failed: {scheme.status_code}")

    return {
        "branch_id": branch_id,
        "product_id": product_id,
        "catalog_price": "25.00",
        "scheme_id": scheme.json()["id"],
        "owner_headers": owner_headers,
        "manager_headers": login(
            client, api_base, manager_phone, password, label=f"login {manager_phone}"
        ),
        "cashier_headers": login(
            client, api_base, cashier_phone, password, label=f"login {cashier_phone}"
        ),
    }


def _post_sale(client: httpx.Client, api_base: str, headers: dict, body: dict) -> httpx.Response:
    return client.post(f"{api_base}/sales", headers=headers, json=body)


def _post_preview(
    client: httpx.Client, api_base: str, headers: dict, body: dict
) -> httpx.Response:
    return client.post(f"{api_base}/sales/price-preview", headers=headers, json=body)


def _base_body(ctx: dict, lines: list[dict], scheme_id: str | None = None) -> dict:
    body: dict = {"branch_id": ctx["branch_id"], "lines": lines}
    if scheme_id is not None:
        body["discount_scheme_id"] = scheme_id
    return body


def _assert_status_with_detail(
    response: httpx.Response,
    expected: int,
    detail_key: str | None,
    *,
    label: str,
) -> None:
    assert_status(response, expected, label=label)
    if detail_key is not None:
        try:
            detail = response.json().get("detail", "")
        except Exception:
            detail = response.text[:300]
        assert_ok(f"{label} detail", detail_key in str(detail), str(detail))


def test_sale_pricing_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix)
    pid = ctx["product_id"]
    catalog = ctx["catalog_price"]

    catalog_line = [sale_line(pid, "1", catalog)]
    manual_discount_line = [sale_line(pid, "1", catalog, discount_pct="10")]
    override_line = [sale_line(pid, "1", "30.00")]
    scheme_line = [sale_line(pid, "2", catalog)]
    manager_combo_line = [sale_line(pid, "1", "30.00", discount_pct="10")]

    sale_ok = _base_body(ctx, catalog_line)
    sale_ok["payments"] = [{"payment_method": "cash", "amount": catalog}]
    _assert_status_with_detail(
        _post_sale(http_client, api_base, ctx["cashier_headers"], sale_ok),
        201,
        None,
        label="POST /sales cashier catalog price no discount",
    )

    sale_manual = _base_body(ctx, manual_discount_line)
    sale_manual["payments"] = [{"payment_method": "cash", "amount": "22.50"}]
    _assert_status_with_detail(
        _post_sale(http_client, api_base, ctx["cashier_headers"], sale_manual),
        403,
        "sales.apply_discount",
        label="POST /sales cashier manual discount denied",
    )

    sale_override = _base_body(ctx, override_line)
    sale_override["payments"] = [{"payment_method": "cash", "amount": "30.00"}]
    _assert_status_with_detail(
        _post_sale(http_client, api_base, ctx["cashier_headers"], sale_override),
        403,
        "sales.override_price",
        label="POST /sales cashier price override denied",
    )

    sale_scheme = _base_body(ctx, scheme_line, ctx["scheme_id"])
    sale_scheme["payments"] = [{"payment_method": "cash", "amount": "45.00"}]
    _assert_status_with_detail(
        _post_sale(http_client, api_base, ctx["cashier_headers"], sale_scheme),
        201,
        None,
        label="POST /sales cashier discount scheme allowed",
    )

    sale_mgr = _base_body(ctx, manager_combo_line)
    sale_mgr["payments"] = [{"payment_method": "cash", "amount": "27.00"}]
    _assert_status_with_detail(
        _post_sale(http_client, api_base, ctx["manager_headers"], sale_mgr),
        201,
        None,
        label="POST /sales manager manual discount and override",
    )

    _assert_status_with_detail(
        _post_preview(
            http_client, api_base, ctx["cashier_headers"], _base_body(ctx, catalog_line)
        ),
        200,
        None,
        label="POST /sales/price-preview cashier catalog price no discount",
    )

    _assert_status_with_detail(
        _post_preview(
            http_client,
            api_base,
            ctx["cashier_headers"],
            _base_body(ctx, manual_discount_line),
        ),
        403,
        "sales.apply_discount",
        label="POST /sales/price-preview cashier manual discount denied",
    )

    _assert_status_with_detail(
        _post_preview(
            http_client,
            api_base,
            ctx["cashier_headers"],
            _base_body(ctx, override_line),
        ),
        403,
        "sales.override_price",
        label="POST /sales/price-preview cashier price override denied",
    )

    _assert_status_with_detail(
        _post_preview(
            http_client,
            api_base,
            ctx["cashier_headers"],
            _base_body(ctx, scheme_line, ctx["scheme_id"]),
        ),
        200,
        None,
        label="POST /sales/price-preview cashier discount scheme allowed",
    )

    _assert_status_with_detail(
        _post_preview(
            http_client,
            api_base,
            ctx["manager_headers"],
            _base_body(ctx, manager_combo_line),
        ),
        200,
        None,
        label="POST /sales/price-preview manager manual discount and override",
    )

    sale_owner = _base_body(ctx, manual_discount_line)
    sale_owner["payments"] = [{"payment_method": "cash", "amount": "22.50"}]
    _assert_status_with_detail(
        _post_sale(http_client, api_base, ctx["owner_headers"], sale_owner),
        201,
        None,
        label="POST /sales owner manual discount allowed",
    )
