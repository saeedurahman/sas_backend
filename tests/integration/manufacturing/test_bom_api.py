"""Phase 2 — BOM CRUD, validation, active uniqueness, and preview."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.services.manufacturing_bom_service import compute_ingredient_qty_for_production
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


def _create_ingredient_product(
    client: httpx.Client,
    tenant: object,
    *,
    name: str,
) -> str:
    return create_product(client, tenant, name=name)  # type: ignore[arg-type]


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


def test_bom_crud_preview_and_active_uniqueness(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_manufacturing_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="620",
        business_name=f"BOM CRUD {unique_suffix}",
    )
    suffix = tenant.suffix

    flour_id = _create_ingredient_product(
        http_client, tenant, name=f"Flour {suffix}"
    )
    sugar_id = _create_ingredient_product(
        http_client, tenant, name=f"Sugar {suffix}"
    )
    finished_id = _create_manufactured_product(
        http_client, tenant, name=f"Cookie Mix {suffix}"
    )

    create_resp = http_client.post(
        f"{api_base}/manufacturing/boms",
        headers=tenant.owner_headers,
        json={
            "product_id": finished_id,
            "name": f"Standard Recipe {suffix}",
            "output_qty": "10",
            "is_active": True,
            "lines": [
                {
                    "ingredient_product_id": flour_id,
                    "qty_required": "5",
                    "wastage_pct": "10",
                    "sort_order": 1,
                },
                {
                    "ingredient_product_id": sugar_id,
                    "qty_required": "2",
                    "wastage_pct": "0",
                    "sort_order": 2,
                },
            ],
        },
    )
    assert_status(create_resp, 201, label="POST /manufacturing/boms")
    bom_v1 = create_resp.json()
    bom_v1_id = bom_v1["id"]
    assert bom_v1["product_id"] == finished_id
    assert bom_v1["is_active"] is True
    assert bom_v1["version"] == 1
    assert len(bom_v1["lines"]) == 2

    list_resp = http_client.get(
        f"{api_base}/manufacturing/boms",
        headers=tenant.owner_headers,
    )
    assert_status(list_resp, 200, label="GET /manufacturing/boms")
    assert any(item["id"] == bom_v1_id for item in list_resp.json())

    by_product = http_client.get(
        f"{api_base}/manufacturing/boms/by-product/{finished_id}",
        headers=tenant.owner_headers,
    )
    assert_status(by_product, 200, label="GET /manufacturing/boms/by-product/{id}")
    assert len(by_product.json()) == 1

    get_resp = http_client.get(
        f"{api_base}/manufacturing/boms/{bom_v1_id}",
        headers=tenant.owner_headers,
    )
    assert_status(get_resp, 200, label="GET /manufacturing/boms/{id}")
    assert get_resp.json()["name"] == f"Standard Recipe {suffix}"

    preview_resp = http_client.post(
        f"{api_base}/manufacturing/boms/preview",
        headers=tenant.owner_headers,
        json={"bom_header_id": bom_v1_id, "qty_to_produce": "20"},
    )
    assert_status(preview_resp, 200, label="POST /manufacturing/boms/preview")
    preview = preview_resp.json()
    assert Decimal(preview["qty_to_produce"]) == Decimal("20")
    flour_line = next(
        line for line in preview["lines"] if line["ingredient_product_id"] == flour_id
    )
    per_unit, total = compute_ingredient_qty_for_production(
        qty_required=Decimal("5"),
        output_qty=Decimal("10"),
        wastage_pct=Decimal("10"),
        qty_to_produce=Decimal("20"),
    )
    assert Decimal(flour_line["qty_per_output_unit"]) == per_unit
    assert Decimal(flour_line["total_qty_required"]) == total

    create_v2 = http_client.post(
        f"{api_base}/manufacturing/boms",
        headers=tenant.owner_headers,
        json={
            "product_id": finished_id,
            "name": f"Revised Recipe {suffix}",
            "output_qty": "10",
            "is_active": True,
            "lines": [
                {
                    "ingredient_product_id": flour_id,
                    "qty_required": "6",
                    "wastage_pct": "0",
                },
            ],
        },
    )
    assert_status(create_v2, 201, label="POST second active BOM")
    bom_v2 = create_v2.json()
    assert bom_v2["version"] == 2
    assert bom_v2["is_active"] is True

    v1_after = http_client.get(
        f"{api_base}/manufacturing/boms/{bom_v1_id}",
        headers=tenant.owner_headers,
    )
    assert_status(v1_after, 200, label="GET v1 after v2 active")
    assert v1_after.json()["is_active"] is False

    active_only = http_client.get(
        f"{api_base}/manufacturing/boms/by-product/{finished_id}",
        headers=tenant.owner_headers,
        params={"active_only": True},
    )
    assert_status(active_only, 200, label="GET by-product active_only")
    active_boms = active_only.json()
    assert len(active_boms) == 1
    assert active_boms[0]["id"] == bom_v2["id"]

    update_resp = http_client.put(
        f"{api_base}/manufacturing/boms/{bom_v2['id']}",
        headers=tenant.owner_headers,
        json={
            "name": f"Revised Recipe Updated {suffix}",
            "lines": [
                {
                    "ingredient_product_id": flour_id,
                    "qty_required": "7",
                    "wastage_pct": "5",
                },
                {
                    "ingredient_product_id": sugar_id,
                    "qty_required": "1",
                    "wastage_pct": "0",
                },
            ],
        },
    )
    assert_status(update_resp, 200, label="PUT /manufacturing/boms/{id}")
    updated = update_resp.json()
    assert updated["name"] == f"Revised Recipe Updated {suffix}"
    assert len(updated["lines"]) == 2
    flour_updated = next(
        line for line in updated["lines"] if line["ingredient_product_id"] == flour_id
    )
    assert flour_updated["qty_required"] == "7.0000"

    delete_resp = http_client.delete(
        f"{api_base}/manufacturing/boms/{bom_v1_id}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_resp, 200, label="DELETE /manufacturing/boms/{id}")

    gone = http_client.get(
        f"{api_base}/manufacturing/boms/{bom_v1_id}",
        headers=tenant.owner_headers,
    )
    assert_status(gone, 404, label="GET deleted BOM")


def test_bom_validation_rejects_non_manufactured_and_self_ingredient(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_manufacturing_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="621",
        business_name=f"BOM Validation {unique_suffix}",
    )
    suffix = tenant.suffix

    standard_id = create_product(
        http_client, tenant, name=f"Standard Widget {suffix}"
    )
    flour_id = _create_ingredient_product(
        http_client, tenant, name=f"Wheat {suffix}"
    )
    manufactured_id = _create_manufactured_product(
        http_client, tenant, name=f"Bread {suffix}"
    )

    bad_product = http_client.post(
        f"{api_base}/manufacturing/boms",
        headers=tenant.owner_headers,
        json={
            "product_id": standard_id,
            "name": "Invalid BOM",
            "lines": [
                {"ingredient_product_id": flour_id, "qty_required": "1"},
            ],
        },
    )
    assert_status(bad_product, 400, label="BOM on standard product")
    assert "manufactured" in bad_product.json()["detail"].lower()

    self_ref = http_client.post(
        f"{api_base}/manufacturing/boms",
        headers=tenant.owner_headers,
        json={
            "product_id": manufactured_id,
            "name": "Self BOM",
            "lines": [
                {"ingredient_product_id": manufactured_id, "qty_required": "1"},
            ],
        },
    )
    assert_status(self_ref, 400, label="BOM self ingredient")
    assert "same" in self_ref.json()["detail"].lower()


def test_bom_feature_flag_and_cashier_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"BOM Perms {unique_suffix}",
        owner_name="Manufacturing Owner",
        phone_prefix="622",
    )
    create_unit(http_client, tenant)

    blocked = http_client.get(
        f"{api_base}/manufacturing/boms",
        headers=tenant.owner_headers,
    )
    assert_status(blocked, 403, label="GET boms without flag")
    assert blocked.json()["detail"] == "Feature not enabled: enable_manufacturing"

    _enable_manufacturing(http_client, api_base, tenant.owner_headers)

    flour_id = _create_ingredient_product(
        http_client, tenant, name=f"Salt {tenant.suffix}"
    )
    finished_id = _create_manufactured_product(
        http_client, tenant, name=f"Seasoning {tenant.suffix}"
    )

    assert tenant.cashier_headers is not None
    cashier_view = http_client.get(
        f"{api_base}/manufacturing/boms",
        headers=tenant.cashier_headers,
    )
    assert_status(cashier_view, 403, label="cashier GET boms")

    cashier_create = http_client.post(
        f"{api_base}/manufacturing/boms",
        headers=tenant.cashier_headers,
        json={
            "product_id": finished_id,
            "name": "Cashier BOM",
            "lines": [
                {"ingredient_product_id": flour_id, "qty_required": "1"},
            ],
        },
    )
    assert_status(cashier_create, 403, label="cashier POST boms")

    manager_create = http_client.post(
        f"{api_base}/manufacturing/boms",
        headers=tenant.manager_headers,
        json={
            "product_id": finished_id,
            "name": "Manager BOM",
            "lines": [
                {"ingredient_product_id": flour_id, "qty_required": "1"},
            ],
        },
    )
    assert_status(manager_create, 201, label="manager POST boms")
