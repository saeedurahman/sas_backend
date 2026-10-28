"""Inventory module permission enforcement (migrated from test_batch2)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant, create_supplier, create_product, create_unit

pytestmark = pytest.mark.integration


def test_inventory_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch2 {unique_suffix}",
        owner_name="Batch2 Owner",
        phone_prefix="320",
    )
    suffix = tenant.suffix
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    branch_id = tenant.branch_id
    assert manager_headers is not None and cashier_headers is not None

    unit_id = create_unit(http_client, tenant)
    product_id = create_product(
        http_client,
        tenant,
        name=f"Inv Product {suffix}",
        unit_id=unit_id,
    )
    supplier_id = create_supplier(http_client, tenant, name=f"Supplier {suffix}")

    stock_q = f"branch_id={branch_id}"
    read_cases = [
        ("GET /stock/movements (owner)", owner_headers, f"{api_base}/stock/movements?{stock_q}", 200),
        ("GET /stock/movements (manager)", manager_headers, f"{api_base}/stock/movements?{stock_q}", 200),
        ("GET /stock/movements (cashier)", cashier_headers, f"{api_base}/stock/movements?{stock_q}", 200),
        ("GET /adjustments (cashier)", cashier_headers, f"{api_base}/adjustments", 200),
        ("GET /purchases/orders (manager)", manager_headers, f"{api_base}/purchases/orders", 200),
        ("GET /purchases/orders (cashier, denied)", cashier_headers, f"{api_base}/purchases/orders", 403),
        ("GET /transfers (manager)", manager_headers, f"{api_base}/transfers", 200),
        ("GET /transfers (cashier, denied)", cashier_headers, f"{api_base}/transfers", 403),
        ("GET /waste (manager)", manager_headers, f"{api_base}/waste", 200),
        ("GET /waste (cashier, denied)", cashier_headers, f"{api_base}/waste", 403),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    adj_body = {
        "branch_id": branch_id,
        "reason": "opening_balance",
        "lines": [
            {
                "product_id": product_id,
                "qty_delta": "10",
                "cost_per_unit": "5.00",
            }
        ],
    }
    assert_status(
        http_client.post(f"{api_base}/adjustments", headers=manager_headers, json=adj_body),
        201,
        label="POST /adjustments (manager)",
    )
    assert_status(
        http_client.post(f"{api_base}/adjustments", headers=cashier_headers, json=adj_body),
        403,
        label="POST /adjustments (cashier, denied)",
    )

    po_body = {
        "supplier_id": supplier_id,
        "branch_id": branch_id,
        "lines": [
            {
                "product_id": product_id,
                "ordered_qty": "5",
                "cost_per_unit": "4.00",
            }
        ],
    }
    assert_status(
        http_client.post(f"{api_base}/purchases/orders", headers=manager_headers, json=po_body),
        201,
        label="POST /purchases/orders (manager)",
    )
    assert_status(
        http_client.post(f"{api_base}/purchases/orders", headers=cashier_headers, json=po_body),
        403,
        label="POST /purchases/orders (cashier, denied)",
    )

    waste_body = {
        "branch_id": branch_id,
        "reason": "damage",
        "lines": [
            {
                "product_id": product_id,
                "qty": "1",
                "cost_per_unit": "5.00",
            }
        ],
    }
    assert_status(
        http_client.post(f"{api_base}/waste", headers=manager_headers, json=waste_body),
        201,
        label="POST /waste (manager)",
    )
    assert_status(
        http_client.post(f"{api_base}/waste", headers=cashier_headers, json=waste_body),
        403,
        label="POST /waste (cashier, denied)",
    )
