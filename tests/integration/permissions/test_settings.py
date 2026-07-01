"""Settings, tax rates, notifications, search permissions (migrated from test_batch7)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import DEFAULT_PASSWORD, build_rbac_tenant, login

pytestmark = pytest.mark.integration


def test_settings_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch7 {unique_suffix}",
        owner_name="Batch7 Owner",
        phone_prefix="370",
    )
    suffix = tenant.suffix
    password = DEFAULT_PASSWORD
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    no_search_phone = f"373{suffix:07d}"
    assert manager_headers is not None and cashier_headers is not None

    no_search_role = http_client.post(
        f"{api_base}/roles",
        headers=owner_headers,
        json={
            "name": "No Search",
            "description": "Auth only for search denial test",
            "permission_keys": ["auth.logout"],
        },
    )
    assert_status(no_search_role, 201, label="create no-search role")
    no_search_role_id = (
        no_search_role.json()["id"] if no_search_role.status_code == 201 else None
    )

    if no_search_role_id:
        ns = http_client.post(
            f"{api_base}/users",
            headers=owner_headers,
            json={
                "full_name": "No Search User",
                "phone": no_search_phone,
                "password": password,
                "role_ids": [no_search_role_id],
            },
        )
        assert_status(ns, 201, label="create no-search user")

    no_search_headers = (
        login(
            http_client,
            api_base,
            no_search_phone,
            password,
            label=f"login {no_search_phone}",
        )
        if no_search_role_id
        else {}
    )

    read_cases = [
        (
            "GET /settings (manager)",
            manager_headers,
            f"{api_base}/settings",
            200,
        ),
        (
            "GET /settings (cashier)",
            cashier_headers,
            f"{api_base}/settings",
            200,
        ),
        (
            "GET /tax-rates (manager)",
            manager_headers,
            f"{api_base}/tax-rates",
            200,
        ),
        (
            "GET /tax-rates (cashier, denied)",
            cashier_headers,
            f"{api_base}/tax-rates",
            403,
        ),
        (
            "GET /notifications (cashier)",
            cashier_headers,
            f"{api_base}/notifications",
            200,
        ),
        (
            "GET /search?q=test (cashier)",
            cashier_headers,
            f"{api_base}/search?q=test",
            200,
        ),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    if no_search_headers:
        assert_status(
            http_client.get(f"{api_base}/search?q=test", headers=no_search_headers),
            403,
            label="GET /search?q=test (no catalog perms, denied)",
        )

    assert_status(
        http_client.put(
            f"{api_base}/settings",
            headers=owner_headers,
            json={
                "setting_key": f"batch7_{suffix}",
                "setting_value": {"enabled": True},
            },
        ),
        200,
        label="PUT /settings (owner)",
    )
    assert_status(
        http_client.put(
            f"{api_base}/settings",
            headers=manager_headers,
            json={
                "setting_key": f"blocked_{suffix}",
                "setting_value": {"enabled": False},
            },
        ),
        403,
        label="PUT /settings (manager, denied)",
    )
    assert_status(
        http_client.put(
            f"{api_base}/settings",
            headers=cashier_headers,
            json={
                "setting_key": f"cashier_blocked_{suffix}",
                "setting_value": {"enabled": False},
            },
        ),
        403,
        label="PUT /settings (cashier, denied)",
    )

    tax = http_client.post(
        f"{api_base}/tax-rates",
        headers=owner_headers,
        json={"name": f"GST {suffix}", "rate": "17"},
    )
    assert_status(tax, 201, label="POST /tax-rates (owner)")
    assert_status(
        http_client.post(
            f"{api_base}/tax-rates",
            headers=manager_headers,
            json={"name": f"Blocked {suffix}", "rate": "5"},
        ),
        403,
        label="POST /tax-rates (manager, denied)",
    )

    assert_status(
        http_client.post(
            f"{api_base}/notifications/check-alerts",
            headers=owner_headers,
        ),
        200,
        label="POST /notifications/check-alerts (owner)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/notifications/check-alerts",
            headers=cashier_headers,
        ),
        200,
        label="POST /notifications/check-alerts (cashier)",
    )
