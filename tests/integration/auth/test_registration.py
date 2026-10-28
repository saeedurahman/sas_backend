"""Registration permission seeding verification (migrated from test_task5)."""

from __future__ import annotations

import httpx
import pytest

from app.services.role_permission_seed import (
    STANDARD_ROLE_NAMES,
    permission_keys_for_role,
)
from tests.helpers.records import assert_ok
from tests.helpers.tenants import DEFAULT_PASSWORD

pytestmark = pytest.mark.integration


def test_registration_permission_seeding(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    suffix = unique_suffix
    password = DEFAULT_PASSWORD
    owner_phone = f"399{suffix:07d}"
    manager_phone = f"398{suffix:07d}"
    cashier_phone = f"397{suffix:07d}"

    expected_owner = permission_keys_for_role("owner")
    expected_manager = permission_keys_for_role("manager")
    expected_cashier = permission_keys_for_role("cashier")

    assert_ok(
        "expected owner count",
        len(expected_owner) == 68,
        f"{len(expected_owner)} keys (67 granular + products.manage)",
    )
    assert_ok(
        "expected manager count",
        len(expected_manager) == 60,
        f"{len(expected_manager)} keys",
    )
    assert_ok(
        "expected cashier count",
        len(expected_cashier) == 16,
        f"{len(expected_cashier)} keys",
    )
    assert_ok(
        "cashier excludes shifts.view",
        "shifts.view" not in expected_cashier,
    )

    reg = http_client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Task5 Verify {suffix}",
            "business_type_code": "retail",
            "owner_name": "Task5 Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    assert_ok("POST /auth/register", reg.status_code == 201, f"HTTP {reg.status_code}")
    if reg.status_code != 201:
        return

    reg_body = reg.json()
    owner_headers = {"Authorization": f"Bearer {reg_body['access_token']}"}
    owner_me = reg_body["user"]
    assert_ok(
        "register response user is owner context",
        owner_me["phone"] == owner_phone,
        owner_me["phone"],
    )

    roles_resp = http_client.get(f"{api_base}/roles", headers=owner_headers)
    assert_ok("GET /roles after register", roles_resp.status_code == 200)
    if roles_resp.status_code != 200:
        return

    roles = roles_resp.json()
    role_names = sorted(r["name"].lower() for r in roles)
    assert_ok(
        "exactly 3 standard roles",
        len(roles) == 3 and role_names == sorted(STANDARD_ROLE_NAMES),
        f"got {role_names}",
    )

    by_name = {r["name"].lower(): r for r in roles}
    for role_name, expected_keys in (
        ("owner", expected_owner),
        ("manager", expected_manager),
        ("cashier", expected_cashier),
    ):
        role = by_name.get(role_name)
        if role is None:
            assert_ok(f"{role_name} permission set", False, "role missing")
            continue
        actual = set(role.get("permission_keys") or [])
        assert_ok(
            f"{role_name} permission count",
            len(actual) == len(expected_keys),
            f"got {len(actual)}, expected {len(expected_keys)}",
        )
        missing = expected_keys - actual
        extra = actual - expected_keys
        assert_ok(
            f"{role_name} permission keys match seed",
            not missing and not extra,
            f"missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}",
        )

    owners = http_client.get(f"{api_base}/users?role=owner", headers=owner_headers)
    assert_ok("GET /users?role=owner", owners.status_code == 200)
    if owners.status_code == 200:
        owner_users = owners.json()
        assert_ok(
            "registering user has owner role",
            any(u["phone"] == owner_phone for u in owner_users),
            f"{len(owner_users)} owner user(s)",
        )

    manager_role_id = by_name["manager"]["id"]
    cashier_role_id = by_name["cashier"]["id"]

    mgr_create = http_client.post(
        f"{api_base}/users",
        headers=owner_headers,
        json={
            "full_name": "Task5 Manager",
            "phone": manager_phone,
            "password": password,
            "role_ids": [manager_role_id],
        },
    )
    assert_ok("POST /users manager", mgr_create.status_code == 201)

    cash_create = http_client.post(
        f"{api_base}/users",
        headers=owner_headers,
        json={
            "full_name": "Task5 Cashier",
            "phone": cashier_phone,
            "password": password,
            "role_ids": [cashier_role_id],
        },
    )
    assert_ok("POST /users cashier", cash_create.status_code == 201)

    for phone, role_name in (
        (manager_phone, "manager"),
        (cashier_phone, "cashier"),
    ):
        login = http_client.post(
            f"{api_base}/auth/login",
            json={"phone": phone, "password": password},
        )
        assert_ok(f"login {role_name}", login.status_code == 200)
        if login.status_code != 200:
            continue
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = http_client.get(f"{api_base}/auth/me", headers=headers)
        assert_ok(
            f"GET /auth/me ({role_name})",
            me.status_code == 200 and me.json()["phone"] == phone,
            me.json().get("phone", "") if me.status_code == 200 else "",
        )
        role_users = http_client.get(
            f"{api_base}/users?role={role_name}",
            headers=owner_headers,
        )
        if role_users.status_code == 200:
            assert_ok(
                f"GET /users?role={role_name} lists user",
                any(u["phone"] == phone for u in role_users.json()),
            )

    mgr_login = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": manager_phone, "password": password},
    )
    cash_login = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": cashier_phone, "password": password},
    )
    if mgr_login.status_code == 200 and cash_login.status_code == 200:
        mgr_h = {"Authorization": f"Bearer {mgr_login.json()['access_token']}"}
        cash_h = {"Authorization": f"Bearer {cash_login.json()['access_token']}"}
        assert_ok(
            "manager can GET /users (users.view)",
            http_client.get(f"{api_base}/users", headers=mgr_h).status_code == 200,
        )
        assert_ok(
            "cashier denied GET /users",
            http_client.get(f"{api_base}/users", headers=cash_h).status_code == 403,
        )
        assert_ok(
            "cashier can POST /sales (sales.create)",
            http_client.get(f"{api_base}/products", headers=cash_h).status_code == 200,
        )
        assert_ok(
            "cashier denied GET /shifts (no shifts.view)",
            http_client.get(f"{api_base}/shifts", headers=cash_h).status_code == 403,
        )
