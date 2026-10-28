"""Dining table active_sale_id enrichment for open dine-in tabs."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_pos_tenant

pytestmark = pytest.mark.integration


def _enable_restaurant_flags(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
) -> None:
    response = client.put(
        f"{api_base}/business/config",
        headers=headers,
        json={
            "config_json": {
                "enable_restaurant": True,
                "enable_table_management": True,
                "enable_kot": True,
            }
        },
    )
    assert_status(response, 200, label="enable restaurant flags")


def test_active_sale_id_on_table_reads_after_open_tab_with_lines(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    """Open tab + add lines (no fire) → table reads expose active_sale_id for resume."""
    tenant = build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="570",
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None
    assert tenant.shift_id is not None

    table_resp = http_client.post(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "table_number": f"RESUME-{tenant.suffix}",
            "capacity": 4,
        },
    )
    assert_status(table_resp, 201, label="create table")
    table_id = table_resp.json()["id"]
    assert table_resp.json().get("active_sale_id") is None

    available = http_client.get(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert_status(available, 200, label="GET table before tab")
    assert available.json()["status"] == "available"
    assert available.json().get("active_sale_id") is None

    open_resp = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.manager_headers,
        json={
            "branch_id": tenant.branch_id,
            "table_id": table_id,
            "register_shift_id": tenant.shift_id,
        },
    )
    assert_status(open_resp, 201, label="open tab")
    sale_id = open_resp.json()["id"]

    add_resp = http_client.post(
        f"{api_base}/sales/{sale_id}/lines",
        headers=tenant.manager_headers,
        json={
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "2",
                    "unit_price": "100.00",
                }
            ]
        },
    )
    assert_status(add_resp, 200, label="add lines without fire")

    by_id = http_client.get(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert_status(by_id, 200, label="GET /restaurant/tables/{id}")
    body = by_id.json()
    assert body["status"] == "occupied"
    assert body["active_sale_id"] == sale_id

    listed = http_client.get(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        params={"branch_id": tenant.branch_id},
    )
    assert_status(listed, 200, label="GET /restaurant/tables")
    listed_table = next(t for t in listed.json() if t["id"] == table_id)
    assert listed_table["active_sale_id"] == sale_id

    layout = http_client.get(
        f"{api_base}/restaurant/floor-layout",
        headers=tenant.owner_headers,
        params={"branch_id": tenant.branch_id},
    )
    assert_status(layout, 200, label="GET /restaurant/floor-layout")
    unassigned = layout.json()["unassigned_tables"]
    layout_table = next(t for t in unassigned if t["id"] == table_id)
    assert layout_table["active_sale_id"] == sale_id

    cancel = http_client.put(
        f"{api_base}/sales/{sale_id}/cancel",
        headers=tenant.manager_headers,
    )
    assert_status(cancel, 200, label="cancel held tab")

    cleared = http_client.get(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert_status(cleared, 200, label="GET table after cancel")
    assert cleared.json()["status"] == "available"
    assert cleared.json().get("active_sale_id") is None


def test_active_sale_id_null_when_no_open_tab(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="571",
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)

    table_resp = http_client.post(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "table_number": f"FREE-{tenant.suffix}",
            "capacity": 2,
        },
    )
    assert_status(table_resp, 201)
    table_id = table_resp.json()["id"]

    get_resp = http_client.get(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert_status(get_resp, 200)
    assert get_resp.json().get("active_sale_id") is None
