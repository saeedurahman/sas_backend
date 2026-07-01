"""Phase 1 manufacturing foundation — permissions catalog and role seed matrix."""

from __future__ import annotations

import httpx
import pytest

from app.services.role_permission_seed import (
    MANUFACTURING_PERMISSION_KEYS,
    permission_keys_for_role,
)
from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant

pytestmark = pytest.mark.integration


def test_permissions_catalog_includes_manufacturing_module(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Manufacturing P1 {unique_suffix}",
        owner_name="Manufacturing Owner",
        phone_prefix="610",
    )
    response = http_client.get(
        f"{api_base}/permissions",
        headers=tenant.owner_headers,
    )
    assert_status(response, 200, label="GET /permissions (owner)")
    payload = response.json()

    manufacturing_module = next(
        (group for group in payload["modules"] if group["module"] == "manufacturing"),
        None,
    )
    assert manufacturing_module is not None, "manufacturing module missing from catalog"

    catalog_keys = {
        item["permission_key"] for item in manufacturing_module["permissions"]
    }
    assert catalog_keys == set(MANUFACTURING_PERMISSION_KEYS)
    assert len(catalog_keys) == 6


def test_standard_role_manufacturing_permission_matrix(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Manufacturing Roles {unique_suffix}",
        owner_name="Manufacturing Owner",
        phone_prefix="611",
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

    assert MANUFACTURING_PERMISSION_KEYS.issubset(owner_keys)
    assert MANUFACTURING_PERMISSION_KEYS.issubset(manager_keys)
    assert MANUFACTURING_PERMISSION_KEYS.isdisjoint(cashier_keys)


def test_permission_keys_for_role_includes_manufacturing_for_owner_manager_only() -> None:
    owner_keys = permission_keys_for_role("owner")
    manager_keys = permission_keys_for_role("manager")
    cashier_keys = permission_keys_for_role("cashier")

    assert MANUFACTURING_PERMISSION_KEYS.issubset(owner_keys)
    assert MANUFACTURING_PERMISSION_KEYS.issubset(manager_keys)
    assert not MANUFACTURING_PERMISSION_KEYS.intersection(cashier_keys)
