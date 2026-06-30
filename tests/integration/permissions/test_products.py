"""Products module permission enforcement (migrated from test_batch1)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status

pytestmark = pytest.mark.integration


def test_products_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    rbac_tenant,
) -> None:
    tenant = rbac_tenant
    suffix = tenant.suffix
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    assert manager_headers is not None and cashier_headers is not None

    unit_resp = http_client.post(
        f"{api_base}/units",
        headers=owner_headers,
        json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
    )
    assert_status(unit_resp, 201, label="owner setup unit")
    unit_id = unit_resp.json()["id"]

    read_cases = [
        ("GET /categories (owner)", owner_headers, f"{api_base}/categories", 200),
        ("GET /categories (manager)", manager_headers, f"{api_base}/categories", 200),
        ("GET /categories (cashier)", cashier_headers, f"{api_base}/categories", 200),
        ("GET /brands (owner)", owner_headers, f"{api_base}/brands", 200),
        ("GET /units (cashier)", cashier_headers, f"{api_base}/units", 200),
        ("GET /products (cashier)", cashier_headers, f"{api_base}/products", 200),
        ("GET /prices/lists (cashier)", cashier_headers, f"{api_base}/prices/lists", 200),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    write_cases = [
        (
            "POST /categories (owner)",
            owner_headers,
            f"{api_base}/categories",
            {"name": f"Cat {suffix}"},
            201,
        ),
        (
            "POST /categories (manager)",
            manager_headers,
            f"{api_base}/categories",
            {"name": f"Cat M {suffix}"},
            201,
        ),
        (
            "POST /categories (cashier, denied)",
            cashier_headers,
            f"{api_base}/categories",
            {"name": f"Cat C {suffix}"},
            403,
        ),
        (
            "POST /prices/lists (owner)",
            owner_headers,
            f"{api_base}/prices/lists",
            {"name": f"Retail {suffix}", "list_type": "retail"},
            201,
        ),
        (
            "POST /prices/lists (manager)",
            manager_headers,
            f"{api_base}/prices/lists",
            {"name": f"Wholesale {suffix}", "list_type": "wholesale"},
            201,
        ),
        (
            "POST /prices/lists (cashier, denied)",
            cashier_headers,
            f"{api_base}/prices/lists",
            {"name": f"Blocked {suffix}", "list_type": "retail"},
            403,
        ),
    ]
    for label, headers, url, body, expected in write_cases:
        assert_status(http_client.post(url, headers=headers, json=body), expected, label=label)

    product_body = {
        "name": f"Product {suffix}",
        "base_unit_id": unit_id,
        "product_type": "standard",
        "tracking_type": "none",
    }
    create_owner = http_client.post(
        f"{api_base}/products",
        headers=owner_headers,
        json=product_body,
    )
    assert_status(create_owner, 201, label="POST /products (owner)")
    product_id = create_owner.json()["id"]

    assert_status(
        http_client.post(
            f"{api_base}/products",
            headers=manager_headers,
            json={
                **product_body,
                "name": f"Product M {suffix}",
                "sku": f"SKU-M-{suffix}",
            },
        ),
        201,
        label="POST /products (manager)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/products",
            headers=cashier_headers,
            json={
                **product_body,
                "name": f"Product C {suffix}",
                "sku": f"SKU-C-{suffix}",
            },
        ),
        403,
        label="POST /products (cashier, denied)",
    )

    assert_status(
        http_client.delete(f"{api_base}/products/{product_id}", headers=manager_headers),
        403,
        label="DELETE /products (manager, denied)",
    )
    assert_status(
        http_client.delete(f"{api_base}/products/{product_id}", headers=cashier_headers),
        403,
        label="DELETE /products (cashier, denied)",
    )
    assert_status(
        http_client.delete(f"{api_base}/products/{product_id}", headers=owner_headers),
        200,
        label="DELETE /products (owner)",
    )
