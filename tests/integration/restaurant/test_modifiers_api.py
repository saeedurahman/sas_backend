"""Phase 3 — modifier groups, modifiers, and product linking integration tests."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_pos_tenant, build_rbac_tenant, create_unit

pytestmark = pytest.mark.integration


def _enable_restaurant(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
) -> None:
    response = client.put(
        f"{api_base}/business/config",
        headers=headers,
        json={"config_json": {"enable_restaurant": True}},
    )
    assert_status(response, 200, label="enable enable_restaurant")


def _setup_product_tenant(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
    *,
    phone_prefix: str,
    business_name: str,
) -> tuple[object, str]:
    tenant = build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=business_name,
        phone_prefix=phone_prefix,
    )
    _enable_restaurant(http_client, api_base, tenant.owner_headers)
    assert tenant.product_id is not None
    return tenant, tenant.product_id


def test_modifier_groups_crud_and_product_link_replace(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant, product_id = _setup_product_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="530",
        business_name=f"Modifiers P3 {unique_suffix}",
    )
    suffix = tenant.suffix

    size_group = http_client.post(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
        json={
            "name": f"Size {suffix}",
            "selection_type": "single",
            "is_required": True,
            "max_selections": 1,
            "sort_order": 1,
        },
    )
    assert_status(size_group, 201, label="POST /restaurant/modifier-groups")
    size_group_id = size_group.json()["id"]

    toppings_group = http_client.post(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
        json={
            "name": f"Toppings {suffix}",
            "selection_type": "multiple",
            "max_selections": 2,
            "sort_order": 2,
        },
    )
    assert_status(toppings_group, 201, label="POST /restaurant/modifier-groups (multiple)")
    toppings_group_id = toppings_group.json()["id"]

    small = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{size_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Small", "price_delta": "0.00", "sort_order": 1},
    )
    assert_status(small, 201, label="POST modifier Small")
    small_id = small.json()["id"]

    large = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{size_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Large", "price_delta": "2.50", "sort_order": 2},
    )
    assert_status(large, 201, label="POST modifier Large")
    large_id = large.json()["id"]

    cheese = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{toppings_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Extra Cheese", "price_delta": "1.00"},
    )
    assert_status(cheese, 201, label="POST modifier Extra Cheese")
    cheese_id = cheese.json()["id"]

    onion = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{toppings_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "No Onion", "price_delta": "0.00"},
    )
    assert_status(onion, 201, label="POST modifier No Onion")
    onion_id = onion.json()["id"]

    list_groups = http_client.get(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
    )
    assert_status(list_groups, 200, label="GET /restaurant/modifier-groups")
    assert len(list_groups.json()) >= 2

    get_group = http_client.get(
        f"{api_base}/restaurant/modifier-groups/{size_group_id}",
        headers=tenant.owner_headers,
    )
    assert_status(get_group, 200, label="GET /restaurant/modifier-groups/{id}")
    assert len(get_group.json()["modifiers"]) == 2

    update_group = http_client.put(
        f"{api_base}/restaurant/modifier-groups/{size_group_id}",
        headers=tenant.owner_headers,
        json={"name": f"Portion {suffix}"},
    )
    assert_status(update_group, 200, label="PUT /restaurant/modifier-groups/{id}")
    assert update_group.json()["name"] == f"Portion {suffix}"

    update_modifier = http_client.put(
        f"{api_base}/restaurant/modifiers/{large_id}",
        headers=tenant.owner_headers,
        json={"price_delta": "3.00"},
    )
    assert_status(update_modifier, 200, label="PUT /restaurant/modifiers/{id}")
    assert update_modifier.json()["price_delta"] == "3.00"

    link_resp = http_client.put(
        f"{api_base}/restaurant/products/{product_id}/modifier-groups",
        headers=tenant.owner_headers,
        json={"modifier_group_ids": [size_group_id, toppings_group_id]},
    )
    assert_status(link_resp, 200, label="PUT product modifier-groups")
    linked = link_resp.json()
    assert len(linked) == 2
    linked_ids = {group["id"] for group in linked}
    assert linked_ids == {size_group_id, toppings_group_id}

    get_linked = http_client.get(
        f"{api_base}/restaurant/products/{product_id}/modifier-groups",
        headers=tenant.owner_headers,
    )
    assert_status(get_linked, 200, label="GET product modifier-groups")
    assert len(get_linked.json()) == 2

    replace_resp = http_client.put(
        f"{api_base}/restaurant/products/{product_id}/modifier-groups",
        headers=tenant.owner_headers,
        json={"modifier_group_ids": [size_group_id]},
    )
    assert_status(replace_resp, 200, label="PUT product modifier-groups replace")
    assert len(replace_resp.json()) == 1
    assert replace_resp.json()[0]["id"] == size_group_id

    valid = http_client.post(
        f"{api_base}/restaurant/modifiers/validate",
        headers=tenant.owner_headers,
        json={"product_id": product_id, "modifier_ids": [large_id]},
    )
    assert_status(valid, 200, label="POST /restaurant/modifiers/validate")
    assert valid.json()[0]["modifier_id"] == large_id
    assert valid.json()[0]["price_delta"] == "3.00"

    delete_modifier = http_client.delete(
        f"{api_base}/restaurant/modifiers/{onion_id}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_modifier, 200, label="DELETE /restaurant/modifiers/{id}")

    delete_group = http_client.delete(
        f"{api_base}/restaurant/modifier-groups/{toppings_group_id}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_group, 200, label="DELETE /restaurant/modifier-groups/{id}")

    # Keep references for validation tests below
    _ = (small_id, large_id, cheese_id)


def test_modifier_validation_errors(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant, product_id = _setup_product_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="531",
        business_name=f"Modifier Validate {unique_suffix}",
    )
    suffix = tenant.suffix

    required_group = http_client.post(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
        json={
            "name": f"Spice {suffix}",
            "selection_type": "single",
            "is_required": True,
            "max_selections": 1,
        },
    )
    assert_status(required_group, 201)
    required_group_id = required_group.json()["id"]

    multi_group = http_client.post(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
        json={
            "name": f"Add-ons {suffix}",
            "selection_type": "multiple",
            "max_selections": 1,
        },
    )
    assert_status(multi_group, 201)
    multi_group_id = multi_group.json()["id"]

    mild = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{required_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Mild", "price_delta": "0.00"},
    ).json()["id"]
    hot = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{required_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Hot", "price_delta": "0.50"},
    ).json()["id"]
    addon_a = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{multi_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Addon A", "price_delta": "1.00"},
    ).json()["id"]
    addon_b = http_client.post(
        f"{api_base}/restaurant/modifier-groups/{multi_group_id}/modifiers",
        headers=tenant.owner_headers,
        json={"name": "Addon B", "price_delta": "1.00"},
    ).json()["id"]

    http_client.put(
        f"{api_base}/restaurant/products/{product_id}/modifier-groups",
        headers=tenant.owner_headers,
        json={"modifier_group_ids": [required_group_id, multi_group_id]},
    )

    required_empty = http_client.post(
        f"{api_base}/restaurant/modifiers/validate",
        headers=tenant.owner_headers,
        json={"product_id": product_id, "modifier_ids": []},
    )
    assert_status(required_empty, 400, label="validate required group empty")
    assert "requires at least" in required_empty.json()["detail"]

    over_max = http_client.post(
        f"{api_base}/restaurant/modifiers/validate",
        headers=tenant.owner_headers,
        json={"product_id": product_id, "modifier_ids": [addon_a, addon_b, mild]},
    )
    assert_status(over_max, 400, label="validate over max selections")
    assert "allows at most" in over_max.json()["detail"]

    wrong_single = http_client.post(
        f"{api_base}/restaurant/modifiers/validate",
        headers=tenant.owner_headers,
        json={"product_id": product_id, "modifier_ids": [mild, hot]},
    )
    assert_status(wrong_single, 400, label="validate single group count")
    detail = wrong_single.json()["detail"]
    assert "only one selection" in detail or "allows at most 1 selection" in detail

    ok = http_client.post(
        f"{api_base}/restaurant/modifiers/validate",
        headers=tenant.owner_headers,
        json={"product_id": product_id, "modifier_ids": [hot, addon_a]},
    )
    assert_status(ok, 200, label="validate valid selection")
    assert len(ok.json()) == 2


def test_modifier_api_403_without_feature_flag(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Modifier Flag {unique_suffix}",
        owner_name="Flag Owner",
        phone_prefix="532",
    )
    response = http_client.get(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
    )
    assert_status(response, 403, label="GET modifier-groups without flag")
    assert response.json()["detail"] == "Feature not enabled: enable_restaurant"


def test_modifier_cashier_view_only_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Modifier Cashier {unique_suffix}",
        owner_name="Cashier Owner",
        phone_prefix="533",
    )
    _enable_restaurant(http_client, api_base, tenant.owner_headers)
    assert tenant.cashier_headers is not None
    create_unit(http_client, tenant)

    product_resp = http_client.post(
        f"{api_base}/products",
        headers=tenant.owner_headers,
        json={
            "name": f"Cashier Product {unique_suffix}",
            "base_unit_id": tenant.unit_id,
            "product_type": "standard",
            "tracking_type": "none",
        },
    )
    assert_status(product_resp, 201)
    product_id = product_resp.json()["id"]

    group_resp = http_client.post(
        f"{api_base}/restaurant/modifier-groups",
        headers=tenant.owner_headers,
        json={"name": f"Cashier Group {unique_suffix}", "selection_type": "optional"},
    )
    assert_status(group_resp, 201)
    group_id = group_resp.json()["id"]

    assert_status(
        http_client.get(
            f"{api_base}/restaurant/modifier-groups",
            headers=tenant.cashier_headers,
        ),
        200,
        label="GET modifier-groups (cashier)",
    )
    assert_status(
        http_client.get(
            f"{api_base}/restaurant/products/{product_id}/modifier-groups",
            headers=tenant.cashier_headers,
        ),
        200,
        label="GET product modifier-groups (cashier)",
    )

    write_cases = [
        (
            "POST modifier-groups",
            "post",
            f"{api_base}/restaurant/modifier-groups",
            {"name": "Blocked"},
        ),
        (
            "PUT modifier-groups",
            "put",
            f"{api_base}/restaurant/modifier-groups/{group_id}",
            {"name": "Blocked Rename"},
        ),
        (
            "DELETE modifier-groups",
            "delete",
            f"{api_base}/restaurant/modifier-groups/{group_id}",
            None,
        ),
        (
            "PUT product modifier-groups",
            "put",
            f"{api_base}/restaurant/products/{product_id}/modifier-groups",
            {"modifier_group_ids": [group_id]},
        ),
    ]
    for label, method, url, payload in write_cases:
        if method == "post":
            response = http_client.post(url, headers=tenant.cashier_headers, json=payload)
        elif method == "put":
            response = http_client.put(url, headers=tenant.cashier_headers, json=payload)
        else:
            response = http_client.delete(url, headers=tenant.cashier_headers)
        assert_status(response, 403, label=f"{label} (cashier)")
