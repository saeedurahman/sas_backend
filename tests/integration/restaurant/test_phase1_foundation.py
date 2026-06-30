"""Phase 1 restaurant foundation — permissions catalog and role seed matrix."""

from __future__ import annotations

import httpx
import pytest

from app.services.role_permission_seed import (
    RESTAURANT_CASHIER_PERMISSION_KEYS,
    RESTAURANT_PERMISSION_KEYS,
    permission_keys_for_role,
)
from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant

pytestmark = pytest.mark.integration


def test_permissions_catalog_includes_restaurant_module(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Restaurant P1 {unique_suffix}",
        owner_name="Restaurant Owner",
        phone_prefix="510",
    )
    response = http_client.get(
        f"{api_base}/permissions",
        headers=tenant.owner_headers,
    )
    assert_status(response, 200, label="GET /permissions (owner)")
    payload = response.json()

    restaurant_module = next(
        (group for group in payload["modules"] if group["module"] == "restaurant"),
        None,
    )
    assert restaurant_module is not None, "restaurant module missing from catalog"

    catalog_keys = {item["permission_key"] for item in restaurant_module["permissions"]}
    assert catalog_keys == set(RESTAURANT_PERMISSION_KEYS)
    assert len(catalog_keys) == 10


def test_standard_role_restaurant_permission_matrix(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Restaurant Roles {unique_suffix}",
        owner_name="Restaurant Owner",
        phone_prefix="511",
    )
    roles_response = http_client.get(
        f"{api_base}/roles",
        headers=tenant.owner_headers,
    )
    assert_status(roles_response, 200, label="GET /roles")
    roles = {role["name"].lower(): role for role in roles_response.json()}

    owner_keys = set(roles["owner"]["permission_keys"])
    manager_keys = set(roles["manager"]["permission_keys"])
    cashier_keys = set(roles["cashier"]["permission_keys"])

    assert RESTAURANT_PERMISSION_KEYS.issubset(owner_keys)
    assert RESTAURANT_PERMISSION_KEYS.issubset(manager_keys)
    assert RESTAURANT_CASHIER_PERMISSION_KEYS.issubset(cashier_keys)

    cashier_restaurant_keys = cashier_keys & RESTAURANT_PERMISSION_KEYS
    assert cashier_restaurant_keys == RESTAURANT_CASHIER_PERMISSION_KEYS

    manager_only_restaurant = RESTAURANT_PERMISSION_KEYS - RESTAURANT_CASHIER_PERMISSION_KEYS
    assert manager_only_restaurant.issubset(manager_keys)
    assert manager_only_restaurant.isdisjoint(cashier_keys)


def test_permission_keys_for_role_matches_seed_constants() -> None:
    owner_keys = permission_keys_for_role("owner")
    manager_keys = permission_keys_for_role("manager")
    cashier_keys = permission_keys_for_role("cashier")

    assert RESTAURANT_PERMISSION_KEYS.issubset(owner_keys)
    assert RESTAURANT_PERMISSION_KEYS.issubset(manager_keys)
    assert RESTAURANT_CASHIER_PERMISSION_KEYS.issubset(cashier_keys)
    assert not (RESTAURANT_PERMISSION_KEYS - RESTAURANT_CASHIER_PERMISSION_KEYS).issubset(
        cashier_keys
    )
