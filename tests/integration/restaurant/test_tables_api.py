"""Phase 2 — floor plan and dine-in table API integration tests."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant

pytestmark = pytest.mark.integration


def _enable_table_management(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
) -> None:
    response = client.put(
        f"{api_base}/business/config",
        headers=headers,
        json={"config_json": {"enable_table_management": True}},
    )
    assert_status(response, 200, label="enable enable_table_management")


def test_tables_api_crud_and_floor_layout(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Restaurant P2 {unique_suffix}",
        owner_name="Restaurant Owner",
        phone_prefix="520",
    )
    _enable_table_management(http_client, api_base, tenant.owner_headers)
    branch_id = tenant.branch_id
    suffix = tenant.suffix

    floor_plan_resp = http_client.post(
        f"{api_base}/restaurant/floor-plans",
        headers=tenant.owner_headers,
        json={
            "branch_id": branch_id,
            "name": f"Main Hall {suffix}",
            "sort_order": 1,
            "layout_json": {"width": 800, "height": 600},
        },
    )
    assert_status(floor_plan_resp, 201, label="POST /restaurant/floor-plans")
    floor_plan = floor_plan_resp.json()
    floor_plan_id = floor_plan["id"]
    assert floor_plan["name"] == f"Main Hall {suffix}"

    table_resp = http_client.post(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        json={
            "branch_id": branch_id,
            "floor_plan_id": floor_plan_id,
            "table_number": f"T-{suffix}",
            "capacity": 6,
            "pos_x": "120.50",
            "pos_y": "80.25",
        },
    )
    assert_status(table_resp, 201, label="POST /restaurant/tables")
    table = table_resp.json()
    table_id = table["id"]
    assert table["status"] == "available"
    assert table["pos_x"] == "120.50"
    assert table["pos_y"] == "80.25"

    unassigned_resp = http_client.post(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        json={
            "branch_id": branch_id,
            "table_number": f"BAR-{suffix}",
            "capacity": 2,
        },
    )
    assert_status(unassigned_resp, 201, label="POST /restaurant/tables (unassigned)")
    unassigned_id = unassigned_resp.json()["id"]

    list_plans = http_client.get(
        f"{api_base}/restaurant/floor-plans",
        headers=tenant.owner_headers,
        params={"branch_id": branch_id},
    )
    assert_status(list_plans, 200, label="GET /restaurant/floor-plans")
    assert any(plan["id"] == floor_plan_id for plan in list_plans.json())

    get_plan = http_client.get(
        f"{api_base}/restaurant/floor-plans/{floor_plan_id}",
        headers=tenant.owner_headers,
    )
    assert_status(get_plan, 200, label="GET /restaurant/floor-plans/{id}")

    update_plan = http_client.put(
        f"{api_base}/restaurant/floor-plans/{floor_plan_id}",
        headers=tenant.owner_headers,
        json={"name": f"Updated Hall {suffix}"},
    )
    assert_status(update_plan, 200, label="PUT /restaurant/floor-plans/{id}")
    assert update_plan.json()["name"] == f"Updated Hall {suffix}"

    list_tables = http_client.get(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        params={"branch_id": branch_id, "floor_plan_id": floor_plan_id},
    )
    assert_status(list_tables, 200, label="GET /restaurant/tables")
    assert len(list_tables.json()) == 1

    get_table = http_client.get(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert_status(get_table, 200, label="GET /restaurant/tables/{id}")

    update_table = http_client.put(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
        json={"capacity": 8, "pos_x": "150.00", "pos_y": "90.00"},
    )
    assert_status(update_table, 200, label="PUT /restaurant/tables/{id}")
    assert update_table.json()["capacity"] == 8

    layout_resp = http_client.get(
        f"{api_base}/restaurant/floor-layout",
        headers=tenant.owner_headers,
        params={"branch_id": branch_id},
    )
    assert_status(layout_resp, 200, label="GET /restaurant/floor-layout")
    layout = layout_resp.json()
    assert layout["branch_id"] == branch_id
    assert len(layout["floor_plans"]) >= 1

    nested_plan = next(
        plan for plan in layout["floor_plans"] if plan["id"] == floor_plan_id
    )
    assert len(nested_plan["tables"]) == 1
    nested_table = nested_plan["tables"][0]
    assert nested_table["id"] == table_id
    assert nested_table["status"] == "available"
    assert nested_table["pos_x"] == "150.00"
    assert nested_table["pos_y"] == "90.00"

    unassigned_ids = {table["id"] for table in layout["unassigned_tables"]}
    assert unassigned_id in unassigned_ids

    delete_table = http_client.delete(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_table, 200, label="DELETE /restaurant/tables/{id}")

    delete_plan = http_client.delete(
        f"{api_base}/restaurant/floor-plans/{floor_plan_id}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_plan, 200, label="DELETE /restaurant/floor-plans/{id}")


def test_tables_api_403_without_feature_flag(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Restaurant Flag {unique_suffix}",
        owner_name="Flag Owner",
        phone_prefix="521",
    )

    response = http_client.get(
        f"{api_base}/restaurant/floor-plans",
        headers=tenant.owner_headers,
        params={"branch_id": tenant.branch_id},
    )
    assert_status(response, 403, label="GET /restaurant/floor-plans without flag")
    assert response.json()["detail"] == "Feature not enabled: enable_table_management"


def test_tables_api_403_without_permission(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Restaurant Perm {unique_suffix}",
        owner_name="Perm Owner",
        phone_prefix="522",
    )
    _enable_table_management(http_client, api_base, tenant.owner_headers)
    assert tenant.cashier_headers is not None

    response = http_client.post(
        f"{api_base}/restaurant/floor-plans",
        headers=tenant.cashier_headers,
        json={
            "branch_id": tenant.branch_id,
            "name": "Blocked Hall",
        },
    )
    assert_status(response, 403, label="POST /restaurant/floor-plans (cashier)")


def test_table_status_patch_validation(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Restaurant Status {unique_suffix}",
        owner_name="Status Owner",
        phone_prefix="523",
    )
    _enable_table_management(http_client, api_base, tenant.owner_headers)
    assert tenant.cashier_headers is not None
    suffix = tenant.suffix

    table_resp = http_client.post(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "table_number": f"ST-{suffix}",
            "capacity": 4,
        },
    )
    assert_status(table_resp, 201, label="POST /restaurant/tables")
    table_id = table_resp.json()["id"]

    invalid = http_client.patch(
        f"{api_base}/restaurant/tables/{table_id}/status",
        headers=tenant.cashier_headers,
        json={"status": "billing"},
    )
    assert_status(invalid, 400, label="PATCH status available→billing (invalid)")
    assert "Invalid table status transition" in invalid.json()["detail"]

    valid = http_client.patch(
        f"{api_base}/restaurant/tables/{table_id}/status",
        headers=tenant.cashier_headers,
        json={"status": "occupied"},
    )
    assert_status(valid, 200, label="PATCH status available→occupied")
    assert valid.json()["status"] == "occupied"

    next_valid = http_client.patch(
        f"{api_base}/restaurant/tables/{table_id}/status",
        headers=tenant.cashier_headers,
        json={"status": "billing"},
    )
    assert_status(next_valid, 200, label="PATCH status occupied→billing")
    assert next_valid.json()["status"] == "billing"

    force_blocked = http_client.patch(
        f"{api_base}/restaurant/tables/{table_id}/status",
        headers=tenant.cashier_headers,
        json={"status": "available", "force": True},
    )
    assert_status(force_blocked, 400, label="PATCH force without manage permission")

    force_allowed = http_client.patch(
        f"{api_base}/restaurant/tables/{table_id}/status",
        headers=tenant.owner_headers,
        json={"status": "available", "force": True},
    )
    assert_status(force_allowed, 200, label="PATCH force with manage permission")
    assert force_allowed.json()["status"] == "available"
