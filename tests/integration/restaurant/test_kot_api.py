"""Phase 5 — kitchen-facing KOT API integration tests."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_scalar
from tests.helpers.tenants import DEFAULT_PASSWORD, build_pos_tenant, login

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


def _setup_table_and_modifier(
    client: httpx.Client,
    api_base: str,
    tenant,
    suffix: int,
) -> tuple[str, str]:
    table_resp = client.post(
        f"{api_base}/restaurant/tables",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "table_number": f"KOT-{suffix}",
            "capacity": 4,
        },
    )
    assert_status(table_resp, 201, label="create table")
    table_id = table_resp.json()["id"]

    group_resp = client.post(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
        json={"name": f"KOT Add-ons {suffix}", "selection_type": "optional"},
    )
    assert_status(group_resp, 201, label="create modifier group")
    group_id = group_resp.json()["id"]

    mod_resp = client.post(
        f"{api_base}/restaurant/modifier-groups/{group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "No Onions", "price_delta": "0.00"},
    )
    assert_status(mod_resp, 201, label="create modifier")
    modifier_id = mod_resp.json()["id"]

    link_resp = client.put(
        f"{api_base}/restaurant/products/{tenant.product_id}/modifier-groups",
        headers=tenant.owner_headers,
        json={"modifier_group_ids": [group_id]},
    )
    assert_status(link_resp, 200, label="link modifier group")
    return table_id, modifier_id


def _fire_kot(
    client: httpx.Client,
    api_base: str,
    tenant,
    *,
    table_id: str,
    modifier_id: str,
    kitchen_notes: str = "Extra crispy",
    headers: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """Open tab, add line with modifiers, fire — returns (sale_id, kot_id, kot_line_id)."""
    assert tenant.product_id is not None
    assert tenant.manager_headers is not None
    auth = headers or tenant.manager_headers

    open_resp = client.post(
        f"{api_base}/sales/open-tab",
        headers=auth,
        json={
            "branch_id": tenant.branch_id,
            "table_id": table_id,
            "register_shift_id": tenant.shift_id,
        },
    )
    assert_status(open_resp, 201, label="open tab")
    sale_id = open_resp.json()["id"]

    add_resp = client.post(
        f"{api_base}/sales/{sale_id}/lines",
        headers=auth,
        json={
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "1",
                    "unit_price": "100.00",
                    "modifier_ids": [modifier_id],
                    "notes": kitchen_notes,
                }
            ]
        },
    )
    assert_status(add_resp, 200, label="add tab lines")

    fire_resp = client.post(
        f"{api_base}/sales/{sale_id}/fire",
        headers=auth,
        json={},
    )
    assert_status(fire_resp, 201, label="fire to kitchen")
    kot_id = fire_resp.json()["id"]

    kot_line_id = db_scalar(
        """
        SELECT id FROM kot_order_lines
        WHERE kot_order_id = :kot_order_id
        LIMIT 1
        """,
        {"kot_order_id": kot_id},
    )
    assert kot_line_id is not None
    return sale_id, kot_id, str(kot_line_id)


def _create_kitchen_staff_headers(
    client: httpx.Client,
    api_base: str,
    tenant,
    suffix: int,
) -> dict[str, str]:
    role_resp = client.post(
        f"{api_base}/roles",
        headers=tenant.owner_headers,
        json={
            "name": f"Kitchen Staff {suffix}",
            "description": "KDS-only role",
            "permission_keys": [
                "auth.login",
                "auth.logout",
                "auth.refresh",
                "restaurant.kot.view",
                "restaurant.kot.update_status",
            ],
        },
    )
    assert_status(role_resp, 201, label="create kitchen role")
    role_id = role_resp.json()["id"]

    kitchen_phone = f"550{suffix:07d}"
    user_resp = client.post(
        f"{api_base}/users",
        headers=tenant.owner_headers,
        json={
            "full_name": "Kitchen Staff",
            "phone": kitchen_phone,
            "password": DEFAULT_PASSWORD,
            "role_ids": [role_id],
        },
    )
    assert_status(user_resp, 201, label="create kitchen user")
    return login(client, api_base, kitchen_phone, DEFAULT_PASSWORD)


def test_active_kot_queue_includes_table_modifiers_and_notes(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client, api_base, unique_suffix, phone_prefix="560"
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    table_id, modifier_id = _setup_table_and_modifier(
        http_client, api_base, tenant, tenant.suffix
    )
    _, kot_id, _ = _fire_kot(
        http_client,
        api_base,
        tenant,
        table_id=table_id,
        modifier_id=modifier_id,
        kitchen_notes="Allergy: nuts",
    )

    active = http_client.get(
        f"{api_base}/restaurant/kot/active",
        headers=tenant.manager_headers,
        params={"branch_id": tenant.branch_id},
    )
    assert_status(active, 200, label="GET /restaurant/kot/active")
    orders = active.json()
    assert len(orders) >= 1
    order = next(o for o in orders if o["id"] == kot_id)
    assert order["table_number"] == f"KOT-{tenant.suffix}"
    assert len(order["lines"]) == 1
    line = order["lines"][0]
    assert line["modifiers_json"][0]["name"] == "No Onions"
    assert line["kitchen_notes"] == "Allergy: nuts"
    assert line["status"] == "pending"

    by_table = http_client.get(
        f"{api_base}/restaurant/kot/by-table/{table_id}",
        headers=tenant.manager_headers,
    )
    assert_status(by_table, 200, label="GET /restaurant/kot/by-table/{id}")
    assert any(o["id"] == kot_id for o in by_table.json())

    by_id = http_client.get(
        f"{api_base}/restaurant/kot/{kot_id}",
        headers=tenant.manager_headers,
    )
    assert_status(by_id, 200, label="GET /restaurant/kot/{id}")
    assert by_id.json()["kot_number"] == order["kot_number"]


def test_kot_line_status_transitions_and_header_timestamps(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client, api_base, unique_suffix, phone_prefix="561"
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    table_id, modifier_id = _setup_table_and_modifier(
        http_client, api_base, tenant, tenant.suffix
    )
    _, kot_id, kot_line_id = _fire_kot(
        http_client,
        api_base,
        tenant,
        table_id=table_id,
        modifier_id=modifier_id,
    )

    def patch_line(status: str) -> dict:
        resp = http_client.patch(
            f"{api_base}/restaurant/kot/lines/{kot_line_id}/status",
            headers=tenant.manager_headers,
            json={"status": status},
        )
        assert_status(resp, 200, label=f"line → {status}")
        return resp.json()

    body = patch_line("preparing")
    assert body["status"] == "preparing"
    assert body["ready_at"] is None
    assert body["served_at"] is None

    body = patch_line("ready")
    assert body["status"] == "ready"
    assert body["ready_at"] is not None
    assert body["served_at"] is None

    body = patch_line("served")
    assert body["status"] == "served"
    assert body["served_at"] is not None

    active = http_client.get(
        f"{api_base}/restaurant/kot/active",
        headers=tenant.manager_headers,
    )
    assert_status(active, 200)
    assert not any(o["id"] == kot_id for o in active.json())

    cancel_table_id, cancel_mod = _setup_table_and_modifier(
        http_client,
        api_base,
        tenant,
        tenant.suffix + 9000,
    )
    _, cancel_kot_id, cancel_line_id = _fire_kot(
        http_client,
        api_base,
        tenant,
        table_id=cancel_table_id,
        modifier_id=cancel_mod,
    )
    cancelled = http_client.patch(
        f"{api_base}/restaurant/kot/lines/{cancel_line_id}/status",
        headers=tenant.manager_headers,
        json={"status": "cancelled"},
    )
    assert_status(cancelled, 200, label="line → cancelled")
    assert cancelled.json()["status"] == "cancelled"


def test_kitchen_staff_can_view_kot_but_not_pos(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_pos_tenant(
        http_client, api_base, unique_suffix, phone_prefix="562"
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    table_id, modifier_id = _setup_table_and_modifier(
        http_client, api_base, tenant, tenant.suffix
    )
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None
    _, kot_id, kot_line_id = _fire_kot(
        http_client,
        api_base,
        tenant,
        table_id=table_id,
        modifier_id=modifier_id,
        headers=tenant.manager_headers,
    )

    kitchen_headers = _create_kitchen_staff_headers(
        http_client, api_base, tenant, tenant.suffix
    )

    list_resp = http_client.get(
        f"{api_base}/restaurant/kot/active",
        headers=kitchen_headers,
    )
    assert_status(list_resp, 200, label="kitchen GET active KOT")

    patch_resp = http_client.patch(
        f"{api_base}/restaurant/kot/lines/{kot_line_id}/status",
        headers=kitchen_headers,
        json={"status": "preparing"},
    )
    assert_status(patch_resp, 200, label="kitchen PATCH line status")

    detail_resp = http_client.get(
        f"{api_base}/restaurant/kot/{kot_id}",
        headers=kitchen_headers,
    )
    assert_status(detail_resp, 200, label="kitchen GET KOT by id")
    assert detail_resp.json()["lines"][0]["status"] == "preparing"

    pos_blocked = http_client.post(
        f"{api_base}/sales",
        headers=kitchen_headers,
        json={
            "branch_id": tenant.branch_id,
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "1",
                    "unit_price": "100.00",
                }
            ],
            "payments": [{"payment_method": "cash", "amount": "100.00"}],
        },
    )
    assert_status(pos_blocked, 403, label="kitchen POST /sales blocked")
    assert "sales.create" in pos_blocked.json()["detail"]


def test_header_status_derived_from_slowest_line(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    """Two-line KOT: header stays pending until every line advances."""
    tenant = build_pos_tenant(
        http_client, api_base, unique_suffix, phone_prefix="563"
    )
    _enable_restaurant_flags(http_client, api_base, tenant.owner_headers)
    table_id, modifier_id = _setup_table_and_modifier(
        http_client, api_base, tenant, tenant.suffix
    )
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None

    open_resp = http_client.post(
        f"{api_base}/sales/open-tab",
        headers=tenant.manager_headers,
        json={"branch_id": tenant.branch_id, "table_id": table_id},
    )
    assert_status(open_resp, 201)
    sale_id = open_resp.json()["id"]

    add_resp = http_client.post(
        f"{api_base}/sales/{sale_id}/lines",
        headers=tenant.manager_headers,
        json={
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "1",
                    "unit_price": "100.00",
                    "modifier_ids": [modifier_id],
                },
                {
                    "product_id": tenant.product_id,
                    "qty": "1",
                    "unit_price": "100.00",
                },
            ]
        },
    )
    assert_status(add_resp, 200)
    fire_resp = http_client.post(
        f"{api_base}/sales/{sale_id}/fire",
        headers=tenant.manager_headers,
        json={},
    )
    assert_status(fire_resp, 201)
    kot_id = fire_resp.json()["id"]

    detail = http_client.get(
        f"{api_base}/restaurant/kot/{kot_id}",
        headers=tenant.manager_headers,
    ).json()
    line_ids = [line["id"] for line in detail["lines"]]
    assert len(line_ids) == 2

    prep_one = http_client.patch(
        f"{api_base}/restaurant/kot/lines/{line_ids[0]}/status",
        headers=tenant.manager_headers,
        json={"status": "preparing"},
    )
    assert_status(prep_one, 200)
    assert prep_one.json()["status"] == "pending"

    prep_both = http_client.patch(
        f"{api_base}/restaurant/kot/lines/{line_ids[1]}/status",
        headers=tenant.manager_headers,
        json={"status": "preparing"},
    )
    assert_status(prep_both, 200)
    assert prep_both.json()["status"] == "preparing"
