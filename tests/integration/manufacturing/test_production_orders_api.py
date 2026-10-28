"""Phase 3 — production order lifecycle (draft, start, cancel)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant, create_product, create_unit

pytestmark = pytest.mark.integration


def _enable_manufacturing(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
) -> None:
    response = client.put(
        f"{api_base}/business/config",
        headers=headers,
        json={"config_json": {"enable_manufacturing": True}},
    )
    assert_status(response, 200, label="enable enable_manufacturing")


def _create_manufactured_product(
    client: httpx.Client,
    tenant: object,
    *,
    name: str,
) -> str:
    response = client.post(
        tenant.url("/products"),  # type: ignore[attr-defined]
        headers=tenant.owner_headers,  # type: ignore[attr-defined]
        json={
            "name": name,
            "base_unit_id": tenant.unit_id,  # type: ignore[attr-defined]
            "product_type": "manufactured",
            "tracking_type": "none",
        },
    )
    assert_status(response, 201, label=f"create manufactured product {name}")
    return response.json()["id"]


def _create_bom(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
    *,
    finished_id: str,
    ingredient_id: str,
    name: str,
) -> str:
    response = client.post(
        f"{api_base}/manufacturing/boms",
        headers=headers,
        json={
            "product_id": finished_id,
            "name": name,
            "output_qty": "1",
            "is_active": True,
            "lines": [
                {"ingredient_product_id": ingredient_id, "qty_required": "2"},
            ],
        },
    )
    assert_status(response, 201, label="create BOM")
    return response.json()["id"]


def _setup_manufacturing_tenant(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
    *,
    phone_prefix: str,
    business_name: str,
) -> object:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=business_name,
        owner_name="Manufacturing Owner",
        phone_prefix=phone_prefix,
    )
    create_unit(http_client, tenant)
    _enable_manufacturing(http_client, api_base, tenant.owner_headers)
    return tenant


def test_production_order_lifecycle_draft_start_cancel(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_manufacturing_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="630",
        business_name=f"Production Lifecycle {unique_suffix}",
    )
    suffix = tenant.suffix

    flour_id = create_product(http_client, tenant, name=f"Flour {suffix}")
    finished_id = _create_manufactured_product(
        http_client, tenant, name=f"Bread {suffix}"
    )
    bom_id = _create_bom(
        http_client,
        api_base,
        tenant.owner_headers,
        finished_id=finished_id,
        ingredient_id=flour_id,
        name=f"Bread BOM {suffix}",
    )

    create_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "bom_header_id": bom_id,
            "qty_to_produce": "50",
            "notes": "Morning batch",
        },
    )
    assert_status(create_resp, 201, label="POST production-orders")
    order = create_resp.json()
    order_id = order["id"]
    assert order["status"] == "draft"
    assert order["qty_to_produce"] == "50.0000"
    assert order["qty_produced"] == "0.0000"
    assert order["production_number"].startswith("PRD-")
    assert order["bom"]["id"] == bom_id
    assert order["started_at"] is None

    update_resp = http_client.put(
        f"{api_base}/manufacturing/production-orders/{order_id}",
        headers=tenant.owner_headers,
        json={"qty_to_produce": "40", "notes": "Adjusted batch"},
    )
    assert_status(update_resp, 200, label="PUT production-orders draft")
    assert update_resp.json()["qty_to_produce"] == "40.0000"
    assert update_resp.json()["notes"] == "Adjusted batch"

    start_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/start",
        headers=tenant.owner_headers,
    )
    assert_status(start_resp, 200, label="POST production-orders start")
    started = start_resp.json()
    assert started["status"] == "in_progress"
    assert started["started_at"] is not None

    double_start = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/start",
        headers=tenant.owner_headers,
    )
    assert_status(double_start, 400, label="start non-draft")

    cancel_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/cancel",
        headers=tenant.owner_headers,
    )
    assert_status(cancel_resp, 200, label="POST production-orders cancel")
    assert cancel_resp.json()["status"] == "cancelled"

    list_resp = http_client.get(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.owner_headers,
        params={"status": "cancelled"},
    )
    assert_status(list_resp, 200, label="GET production-orders")
    assert any(item["id"] == order_id for item in list_resp.json())


def test_production_order_requires_active_bom_and_draft_only_update(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_manufacturing_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="631",
        business_name=f"Production Validation {unique_suffix}",
    )
    suffix = tenant.suffix

    flour_id = create_product(http_client, tenant, name=f"Yeast {suffix}")
    finished_id = _create_manufactured_product(
        http_client, tenant, name=f"Roll {suffix}"
    )
    bom_id = _create_bom(
        http_client,
        api_base,
        tenant.owner_headers,
        finished_id=finished_id,
        ingredient_id=flour_id,
        name=f"Roll BOM {suffix}",
    )

    create_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "bom_header_id": bom_id,
            "qty_to_produce": "10",
        },
    )
    assert_status(create_resp, 201, label="create order")
    order_id = create_resp.json()["id"]

    http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/start",
        headers=tenant.owner_headers,
    )

    update_after_start = http_client.put(
        f"{api_base}/manufacturing/production-orders/{order_id}",
        headers=tenant.owner_headers,
        json={"qty_to_produce": "5"},
    )
    assert_status(update_after_start, 400, label="update non-draft")

    inactive_bom = http_client.post(
        f"{api_base}/manufacturing/boms",
        headers=tenant.owner_headers,
        json={
            "product_id": finished_id,
            "name": f"Roll BOM v2 {suffix}",
            "is_active": True,
            "lines": [
                {"ingredient_product_id": flour_id, "qty_required": "3"},
            ],
        },
    )
    assert_status(inactive_bom, 201, label="new active BOM")

    bad_create = http_client.post(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "bom_header_id": bom_id,
            "qty_to_produce": "5",
        },
    )
    assert_status(bad_create, 400, label="create with inactive BOM")
    assert "active" in bad_create.json()["detail"].lower()


def test_production_order_permissions_and_feature_flag(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Production Perms {unique_suffix}",
        owner_name="Manufacturing Owner",
        phone_prefix="632",
    )
    create_unit(http_client, tenant)

    blocked = http_client.get(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.owner_headers,
    )
    assert_status(blocked, 403, label="GET without flag")
    assert blocked.json()["detail"] == "Feature not enabled: enable_manufacturing"

    _enable_manufacturing(http_client, api_base, tenant.owner_headers)

    suffix = tenant.suffix
    flour_id = create_product(http_client, tenant, name=f"Salt {suffix}")
    finished_id = _create_manufactured_product(
        http_client, tenant, name=f"Spice Mix {suffix}"
    )
    bom_id = _create_bom(
        http_client,
        api_base,
        tenant.owner_headers,
        finished_id=finished_id,
        ingredient_id=flour_id,
        name=f"Spice BOM {suffix}",
    )

    assert tenant.cashier_headers is not None
    cashier_list = http_client.get(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.cashier_headers,
    )
    assert_status(cashier_list, 403, label="cashier GET production-orders")

    manager_create = http_client.post(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.manager_headers,
        json={
            "branch_id": tenant.branch_id,
            "bom_header_id": bom_id,
            "qty_to_produce": "1",
        },
    )
    assert_status(manager_create, 201, label="manager create production order")
    order_id = manager_create.json()["id"]

    cashier_cancel = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/cancel",
        headers=tenant.cashier_headers,
    )
    assert_status(cashier_cancel, 403, label="cashier cancel")

    owner_cancel = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/cancel",
        headers=tenant.owner_headers,
    )
    assert_status(owner_cancel, 200, label="owner cancel")
