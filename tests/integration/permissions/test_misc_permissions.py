"""Miscellaneous permission checks (migrated from test_batch9 + test_fix3)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.records import assert_ok
from tests.helpers.tenants import DEFAULT_PASSWORD, build_rbac_tenant

pytestmark = pytest.mark.integration


def test_misc_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch9 {unique_suffix}",
        owner_name="Batch9 Owner",
        phone_prefix="390",
    )
    suffix = tenant.suffix
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    branch_id = tenant.branch_id
    assert manager_headers is not None and cashier_headers is not None

    assert_status(
        http_client.get(f"{api_base}/business/types"),
        200,
        label="GET /business/types (public)",
    )

    reg_body = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": tenant.owner_phone, "password": DEFAULT_PASSWORD},
    ).json()
    owner_refresh = reg_body["refresh_token"]

    cashier_login = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": tenant.cashier_phone, "password": DEFAULT_PASSWORD},
    ).json()
    cashier_refresh = cashier_login["refresh_token"]

    read_cases = [
        (
            "GET /discounts (manager)",
            manager_headers,
            f"{api_base}/discounts",
            200,
        ),
        (
            "GET /discounts (cashier)",
            cashier_headers,
            f"{api_base}/discounts",
            200,
        ),
        (
            "GET /branches (cashier)",
            cashier_headers,
            f"{api_base}/branches",
            200,
        ),
        (
            "GET /business/me (cashier)",
            cashier_headers,
            f"{api_base}/business/me",
            200,
        ),
        (
            "GET /audit (owner)",
            owner_headers,
            f"{api_base}/audit",
            200,
        ),
        (
            "GET /audit (manager, denied)",
            manager_headers,
            f"{api_base}/audit",
            403,
        ),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    assert_status(
        http_client.post(
            f"{api_base}/discounts",
            headers=owner_headers,
            json={
                "name": f"Scheme {suffix}",
                "discount_type": "percentage",
                "discount_value": "10",
            },
        ),
        201,
        label="POST /discounts (owner)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/discounts",
            headers=manager_headers,
            json={
                "name": f"Blocked {suffix}",
                "discount_type": "percentage",
                "discount_value": "5",
            },
        ),
        403,
        label="POST /discounts (manager, denied)",
    )

    assert_status(
        http_client.put(
            f"{api_base}/branches/{branch_id}",
            headers=manager_headers,
            json={"name": f"Main Updated {suffix}"},
        ),
        200,
        label="PUT /branches/{id} (manager)",
    )
    assert_status(
        http_client.put(
            f"{api_base}/branches/{branch_id}",
            headers=cashier_headers,
            json={"name": "Blocked"},
        ),
        403,
        label="PUT /branches/{id} (cashier, denied)",
    )

    assert_status(
        http_client.put(
            f"{api_base}/business/me",
            headers=manager_headers,
            json={"name": "Blocked Business"},
        ),
        403,
        label="PUT /business/me (manager, denied)",
    )

    assert_status(
        http_client.post(
            f"{api_base}/auth/logout",
            headers=cashier_headers,
            json={"refresh_token": cashier_refresh},
        ),
        200,
        label="POST /auth/logout (cashier)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/auth/logout",
            headers=owner_headers,
            json={"refresh_token": owner_refresh},
        ),
        200,
        label="POST /auth/logout (owner)",
    )


def test_discounts_view_permission(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Fix3 {unique_suffix}",
        owner_name="Fix3 Owner",
        phone_prefix="380",
    )
    suffix = tenant.suffix
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    assert manager_headers is not None and cashier_headers is not None

    create = http_client.post(
        f"{api_base}/discounts",
        headers=owner_headers,
        json={
            "name": f"Promo {suffix}",
            "discount_type": "percentage",
            "discount_value": "10",
        },
    )
    assert_status(create, 201, label="POST /discounts (owner setup)")
    scheme_id = create.json()["id"] if create.status_code == 201 else None

    assert_status(
        http_client.get(f"{api_base}/discounts", headers=owner_headers),
        200,
        label="GET /discounts (owner)",
    )
    assert_status(
        http_client.get(f"{api_base}/discounts", headers=manager_headers),
        200,
        label="GET /discounts (manager)",
    )
    assert_status(
        http_client.get(f"{api_base}/discounts", headers=cashier_headers),
        200,
        label="GET /discounts (cashier)",
    )

    if scheme_id:
        assert_status(
            http_client.get(
                f"{api_base}/discounts/{scheme_id}",
                headers=cashier_headers,
            ),
            200,
            label="GET /discounts/{id} (cashier)",
        )

    me = http_client.get(f"{api_base}/auth/me", headers=cashier_headers)
    has_key = (
        me.status_code == 200
        and "discounts.view" in me.json().get("permission_keys", [])
    )
    assert_ok(
        "cashier /auth/me has discounts.view",
        has_key,
        "found" if has_key else "missing",
    )

    assert_status(
        http_client.post(
            f"{api_base}/discounts",
            headers=manager_headers,
            json={
                "name": "Blocked",
                "discount_type": "percentage",
                "discount_value": "5",
            },
        ),
        403,
        label="POST /discounts (manager, denied)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/discounts",
            headers=cashier_headers,
            json={
                "name": "Blocked",
                "discount_type": "percentage",
                "discount_value": "5",
            },
        ),
        403,
        label="POST /discounts (cashier, denied)",
    )

    if scheme_id:
        assert_status(
            http_client.put(
                f"{api_base}/discounts/{scheme_id}",
                headers=manager_headers,
                json={"discount_value": "15"},
            ),
            403,
            label="PUT /discounts/{id} (manager, denied)",
        )
        assert_status(
            http_client.put(
                f"{api_base}/discounts/{scheme_id}",
                headers=cashier_headers,
                json={"discount_value": "15"},
            ),
            403,
            label="PUT /discounts/{id} (cashier, denied)",
        )
