"""Sales module permission enforcement (migrated from test_batch3)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import add_stock_adjustment, build_rbac_tenant, create_product, create_unit

pytestmark = pytest.mark.integration


def test_sales_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch3 {unique_suffix}",
        owner_name="Batch3 Owner",
        phone_prefix="330",
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
        name=f"Sale Product {suffix}",
        unit_id=unit_id,
    )
    add_stock_adjustment(
        http_client,
        tenant,
        product_id=product_id,
        qty_delta="100",
        cost_per_unit="10.00",
    )

    read_cases = [
        ("GET /sales (cashier)", cashier_headers, f"{api_base}/sales", 200),
        ("GET /customers (cashier)", cashier_headers, f"{api_base}/customers", 200),
        ("GET /returns (manager)", manager_headers, f"{api_base}/returns", 200),
        ("GET /returns (cashier, denied)", cashier_headers, f"{api_base}/returns", 403),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    cust = http_client.post(
        f"{api_base}/customers",
        headers=manager_headers,
        json={"name": f"Customer {suffix}"},
    )
    assert_status(cust, 201, label="POST /customers (manager)")
    customer_id = cust.json()["id"]

    assert_status(
        http_client.post(
            f"{api_base}/customers",
            headers=cashier_headers,
            json={"name": f"Cashier Customer {suffix}"},
        ),
        201,
        label="POST /customers (cashier)",
    )

    assert_status(
        http_client.get(f"{api_base}/customers/{customer_id}/ledger", headers=manager_headers),
        200,
        label="GET /customers/{id}/ledger (manager)",
    )
    assert_status(
        http_client.get(f"{api_base}/customers/{customer_id}/ledger", headers=cashier_headers),
        403,
        label="GET /customers/{id}/ledger (cashier, denied)",
    )
    assert_status(
        http_client.put(
            f"{api_base}/customers/{customer_id}",
            headers=manager_headers,
            json={"name": f"Customer Updated {suffix}"},
        ),
        200,
        label="PUT /customers/{id} (manager)",
    )
    assert_status(
        http_client.put(
            f"{api_base}/customers/{customer_id}",
            headers=cashier_headers,
            json={"name": "Blocked"},
        ),
        403,
        label="PUT /customers/{id} (cashier, denied)",
    )
    payment_body = {"amount": "50.00", "payment_method": "cash"}
    assert_status(
        http_client.post(
            f"{api_base}/customers/{customer_id}/payments",
            headers=manager_headers,
            json=payment_body,
        ),
        201,
        label="POST /customers/{id}/payments (manager)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/customers/{customer_id}/payments",
            headers=cashier_headers,
            json=payment_body,
        ),
        403,
        label="POST /customers/{id}/payments (cashier, denied)",
    )

    sale_body = {
        "branch_id": branch_id,
        "lines": [{"product_id": product_id, "qty": "1", "unit_price": "25.00"}],
        "payments": [{"payment_method": "cash", "amount": "25.00"}],
    }
    sale_mgr = http_client.post(f"{api_base}/sales", headers=manager_headers, json=sale_body)
    assert_status(sale_mgr, 201, label="POST /sales (manager)")
    sale_id = sale_mgr.json()["id"]

    assert_status(
        http_client.post(
            f"{api_base}/sales",
            headers=cashier_headers,
            json={
                **sale_body,
                "payments": [{"payment_method": "cash", "amount": "30.00"}],
            },
        ),
        201,
        label="POST /sales (cashier)",
    )

    assert_status(
        http_client.get(f"{api_base}/invoice/{sale_id}", headers=cashier_headers),
        200,
        label="GET /invoice/{sale_id} (cashier)",
    )
    assert_status(
        http_client.get(f"{api_base}/invoice/{sale_id}/thermal", headers=cashier_headers),
        200,
        label="GET /invoice/{sale_id}/thermal (cashier)",
    )

    draft_sale_body = {
        "branch_id": branch_id,
        "lines": [{"product_id": product_id, "qty": "1", "unit_price": "15.00"}],
        "payments": [],
    }
    draft_for_cashier = http_client.post(
        f"{api_base}/sales",
        headers=manager_headers,
        json=draft_sale_body,
    )
    if draft_for_cashier.status_code == 201:
        draft_id = draft_for_cashier.json()["id"]
        assert_status(
            http_client.put(f"{api_base}/sales/{draft_id}/cancel", headers=cashier_headers),
            403,
            label="PUT /sales/{id}/cancel (cashier, denied)",
        )

    draft_for_manager = http_client.post(
        f"{api_base}/sales",
        headers=manager_headers,
        json=draft_sale_body,
    )
    if draft_for_manager.status_code == 201:
        assert_status(
            http_client.put(
                f"{api_base}/sales/{draft_for_manager.json()['id']}/cancel",
                headers=manager_headers,
            ),
            200,
            label="PUT /sales/{id}/cancel (manager)",
        )
