"""Phase 4 — dine-in tab sales integration tests."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_rows, db_scalar
from tests.helpers.sales import create_sale_payload
from tests.helpers.tenants import build_pos_tenant

pytestmark = pytest.mark.integration


def _enable_restaurant_flags(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
    *,
    restaurant: bool = True,
    tables: bool = True,
    kot: bool = True,
) -> None:
    flags = {
        "enable_restaurant": restaurant,
        "enable_table_management": tables,
        "enable_kot": kot,
    }
    response = client.put(
        f"{api_base}/business/config",
        headers=headers,
        json={"config_json": flags},
    )
    assert_status(response, 200, label="set restaurant feature flags")


def _sale_stock_qty(client: httpx.Client, tenant, product_id: str) -> Decimal:
    return Decimal(
        str(
            db_scalar(
                """
                SELECT COALESCE(SUM(qty), 0)
                FROM stock_movements
                WHERE business_id = :business_id
                  AND product_id = :product_id
                  AND deleted_at IS NULL
                """,
                {"business_id": tenant.business_id, "product_id": product_id},
            )
        )
    )


def _setup_table_and_modifiers(
    client: httpx.Client,
    api_base: str,
    tenant,
    suffix: int,
) -> tuple[str, str, str]:
    table_resp = client.post(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "table_number": f"TAB-{suffix}",
            "capacity": 4,
        },
    )
    assert_status(table_resp, 201, label="create table")
    table_id = table_resp.json()["id"]

    group_resp = client.post(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
        json={
            "name": f"Add-ons {suffix}",
            "selection_type": "optional",
        },
    )
    assert_status(group_resp, 201, label="create modifier group")
    group_id = group_resp.json()["id"]

    mod_resp = client.post(
        f"{api_base}/restaurant/modifier-groups/{group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Extra Cheese", "price_delta": "5.00"},
    )
    assert_status(mod_resp, 201, label="create modifier")
    modifier_id = mod_resp.json()["id"]

    link_resp = client.put(
        f"{api_base}/restaurant/products/{tenant.product_id}/modifier-groups",
        headers=tenant.owner_headers,
        json={"modifier_group_ids": [group_id]},
    )
    assert_status(link_resp, 200, label="link modifier group to product")

    return table_id, modifier_id, group_id


def test_tab_full_flow_stock_deferred_and_kot_linkage(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="540",
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    assert tenant.product_id is not None
    assert tenant.shift_id is not None
    assert tenant.manager_headers is not None

    stock_before = _sale_stock_qty(http_client, tenant, tenant.product_id)
    table_id, modifier_id, _ = _setup_table_and_modifiers(
        http_client, api_base, tenant, tenant.suffix
    )

    open_resp = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.manager_headers,
        json={
            "branch_id": tenant.branch_id,
            "table_id": table_id,
            "register_shift_id": tenant.shift_id,
        },
    )
    assert_status(open_resp, 201, label="POST /sales/open-tab")
    sale_id = open_resp.json()["id"]
    assert open_resp.json()["status"] == "held"
    assert open_resp.json()["table_id"] == table_id

    stock_after_open = _sale_stock_qty(http_client, tenant, tenant.product_id)
    assert stock_after_open == stock_before

    add_resp = http_client.post(
        f"{api_base}/sales/{sale_id}/lines",
        headers=tenant.manager_headers,
        json={
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "2",
                    "unit_price": "100.00",
                    "modifier_ids": [modifier_id],
                }
            ]
        },
    )
    assert_status(add_resp, 200, label="POST /sales/{id}/lines")
    sale_line_id = add_resp.json()["lines"][0]["id"]
    assert Decimal(str(add_resp.json()["lines"][0]["unit_price"])) == Decimal("105.00")

    stock_after_lines = _sale_stock_qty(http_client, tenant, tenant.product_id)
    assert stock_after_lines == stock_before

    fire_resp = http_client.post(
        f"{api_base}/sales/{sale_id}/fire",
        headers=tenant.manager_headers,
        json={},
    )
    assert_status(fire_resp, 201, label="POST /sales/{id}/fire")
    kot_id = fire_resp.json()["id"]

    kot_lines = db_rows(
        """
        SELECT sale_line_id, modifiers_json
        FROM kot_order_lines
        WHERE business_id = :business_id
          AND kot_order_id = :kot_order_id
        """,
        {"business_id": tenant.business_id, "kot_order_id": kot_id},
    )
    assert len(kot_lines) == 1
    assert str(kot_lines[0]["sale_line_id"]) == sale_line_id
    assert kot_lines[0]["modifiers_json"][0]["name"] == "Extra Cheese"

    stock_after_fire = _sale_stock_qty(http_client, tenant, tenant.product_id)
    assert stock_after_fire == stock_before

    complete_resp = http_client.post(
        f"{api_base}/sales/{sale_id}/complete",
        headers=tenant.manager_headers,
        json={
            "payments": [{"payment_method": "cash", "amount": "210.00"}],
        },
    )
    assert_status(complete_resp, 200, label="POST /sales/{id}/complete")
    assert complete_resp.json()["status"] == "completed"

    stock_after_complete = _sale_stock_qty(http_client, tenant, tenant.product_id)
    assert stock_after_complete == stock_before - Decimal("2")


def test_second_open_tab_on_same_table_rejected(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="541",
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    table_id, _, _ = _setup_table_and_modifiers(
        http_client, api_base, tenant, tenant.suffix
    )

    first = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.owner_headers,
        json={"branch_id": tenant.branch_id, "table_id": table_id},
    )
    assert_status(first, 201, label="first open-tab")

    second = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.owner_headers,
        json={"branch_id": tenant.branch_id, "table_id": table_id},
    )
    assert_status(second, 400, label="second open-tab rejected")
    assert "active tab" in second.json()["detail"]


def test_tab_feature_flag_403_matrix(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="542",
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    table_id, _, _ = _setup_table_and_modifiers(
        http_client, api_base, tenant, tenant.suffix
    )

    disabled = http_client.put(
        f"{api_base}/business/config",
        headers=tenant.owner_headers,
        json={"config_json": {"enable_restaurant": False}},
    )
    assert_status(disabled, 200)
    blocked_open = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.owner_headers,
        json={"branch_id": tenant.branch_id, "table_id": table_id},
    )
    assert_status(blocked_open, 403, label="open-tab without enable_restaurant")

    _enable_restaurant_flags(
        http_client, api_base, tenant.owner_headers, kot=False
    )
    open_resp = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.owner_headers,
        json={"branch_id": tenant.branch_id, "table_id": table_id},
    )
    assert_status(open_resp, 201, label="open-tab with restaurant+tables only")
    sale_id = open_resp.json()["id"]

    assert tenant.product_id is not None
    add_resp = http_client.post(
        f"{api_base}/sales/{sale_id}/lines",
        headers=tenant.owner_headers,
        json={
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "1",
                    "unit_price": "100.00",
                }
            ]
        },
    )
    assert_status(add_resp, 200, label="add lines before fire")

    fire_blocked = http_client.post(
        f"{api_base}/sales/{sale_id}/fire",
        headers=tenant.owner_headers,
        json={},
    )
    assert_status(fire_blocked, 403, label="fire without enable_kot")
    assert fire_blocked.json()["detail"] == "Feature not enabled: enable_kot"


def test_cancel_held_tab_frees_table(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="543",
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    table_id, _, _ = _setup_table_and_modifiers(
        http_client, api_base, tenant, tenant.suffix
    )

    open_resp = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.owner_headers,
        json={"branch_id": tenant.branch_id, "table_id": table_id},
    )
    assert_status(open_resp, 201)
    sale_id = open_resp.json()["id"]

    occupied = http_client.get(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert occupied.json()["status"] == "occupied"

    cancel = http_client.put(
        f"{api_base}/sales/{sale_id}/cancel",
        headers=tenant.owner_headers,
    )
    assert_status(cancel, 200, label="cancel held tab")
    assert cancel.json()["status"] == "cancelled"

    table = http_client.get(
        f"{api_base}/restaurant/tables/{table_id}",
        headers=tenant.owner_headers,
    )
    assert_status(table, 200)
    assert table.json()["status"] == "available"


def test_non_restaurant_sale_unchanged(
    http_client: httpx.Client,
    api_base: str,
    pos_tenant,
) -> None:
    tenant = pos_tenant
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None
    assert tenant.shift_id is not None

    stock_before = _sale_stock_qty(http_client, tenant, tenant.product_id)

    sale = http_client.post(
        f"{api_base}/sales",
        headers=tenant.manager_headers,
        json=create_sale_payload(
            branch_id=tenant.branch_id,
            product_id=tenant.product_id,
            register_shift_id=tenant.shift_id,
        ),
    )
    assert_status(sale, 201, label="regular POS sale")
    assert sale.json()["status"] == "completed"
    assert sale.json().get("table_id") is None

    stock_after = _sale_stock_qty(http_client, tenant, tenant.product_id)
    assert stock_after == stock_before - Decimal("1")
