"""Auth /me business slug, PIN login, and roles (migrated from auth_me + fix2)."""

from __future__ import annotations

import re

import httpx
import pytest

from app.services.role_permission_seed import permission_keys_for_role
from tests.helpers.records import assert_ok
from tests.helpers.tenants import DEFAULT_PASSWORD, login, register_owner

pytestmark = pytest.mark.integration


def slugify_business_name(name: str) -> str:
    value = name.lower().strip()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"[\s_]+", "-", value).strip("-")
    return value[:200] or "business"


def test_auth_me_business_slug_and_pin(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    suffix = unique_suffix
    password = DEFAULT_PASSWORD
    pin = "1234"
    owner_phone = f"410{suffix:07d}"
    business_name = f"Slug Test {suffix}"

    reg = http_client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": business_name,
            "business_type_code": "retail",
            "owner_name": "Slug Owner",
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
    owner_id = reg_body["user"]["id"]
    reg_user_slug = reg_body["user"].get("business_slug")

    login_resp = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": owner_phone, "password": password},
    )
    assert_ok("POST /auth/login", login_resp.status_code == 200, f"HTTP {login_resp.status_code}")
    login_headers = {
        "Authorization": f"Bearer {login_resp.json()['access_token']}"
    }

    me = http_client.get(f"{api_base}/auth/me", headers=login_headers)
    assert_ok("GET /auth/me", me.status_code == 200, f"HTTP {me.status_code}")
    if me.status_code != 200:
        return

    me_body = me.json()
    slug = me_body.get("business_slug")
    expected_base = slugify_business_name(business_name)
    slug_ok = bool(slug) and (
        slug == expected_base or slug.startswith(f"{expected_base}-")
    )
    assert_ok(
        "GET /auth/me business_slug present and plausible",
        slug_ok,
        f"slug={slug!r} expected_base={expected_base!r}",
    )
    assert_ok(
        "register TokenResponse.user.business_slug matches /auth/me",
        reg_user_slug == slug,
        f"register={reg_user_slug!r} me={slug!r}",
    )

    pin_set = http_client.put(
        f"{api_base}/users/{owner_id}/pin",
        headers=owner_headers,
        json={"pin_code": pin},
    )
    assert_ok(
        "PUT /users/{id}/pin",
        pin_set.status_code == 200,
        f"HTTP {pin_set.status_code}",
    )

    pin_login = http_client.post(
        f"{api_base}/auth/login/pin",
        json={
            "business_slug": slug,
            "user_id": owner_id,
            "pin_code": pin,
        },
    )
    assert_ok(
        "POST /auth/login/pin",
        pin_login.status_code == 200,
        f"HTTP {pin_login.status_code}",
    )
    if pin_login.status_code == 200:
        pin_body = pin_login.json()
        pin_user = pin_body.get("user", {})
        assert_ok(
            "PIN login returns access_token",
            bool(pin_body.get("access_token")),
        )
        assert_ok(
            "PIN login user.business_slug matches",
            pin_user.get("business_slug") == slug,
            f"pin_slug={pin_user.get('business_slug')!r}",
        )
        assert_ok(
            "PIN login user.id matches owner",
            pin_user.get("id") == owner_id,
        )

    bad_pin = http_client.post(
        f"{api_base}/auth/login/pin",
        json={
            "business_slug": slug,
            "user_id": owner_id,
            "pin_code": "9999",
        },
    )
    assert_ok(
        "POST /auth/login/pin wrong PIN denied",
        bad_pin.status_code == 401,
        f"HTTP {bad_pin.status_code}",
    )

    bad_slug = http_client.post(
        f"{api_base}/auth/login/pin",
        json={
            "business_slug": "nonexistent-slug-xyz",
            "user_id": owner_id,
            "pin_code": pin,
        },
    )
    assert_ok(
        "POST /auth/login/pin unknown slug denied",
        bad_slug.status_code == 404,
        f"HTTP {bad_slug.status_code}",
    )


def _check_me(
    http_client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
    *,
    label: str,
    expected_roles: list[str],
    expected_permission_count: int,
    expected_permission_keys: set[str] | None = None,
) -> None:
    resp = http_client.get(f"{api_base}/auth/me", headers=headers)
    if resp.status_code != 200:
        assert_ok(f"GET /auth/me ({label})", False, f"HTTP {resp.status_code}")
        return
    body = resp.json()
    roles = body.get("roles", [])
    keys = body.get("permission_keys", [])
    roles_ok = roles == expected_roles
    count_ok = len(keys) == expected_permission_count
    keys_ok = True
    if expected_permission_keys is not None:
        keys_ok = set(keys) == expected_permission_keys
    assert_ok(
        f"GET /auth/me ({label}) roles",
        roles_ok,
        f"roles={roles!r} expected={expected_roles!r}",
    )
    assert_ok(
        f"GET /auth/me ({label}) permission count",
        count_ok,
        f"count={len(keys)} expected={expected_permission_count}",
    )
    if expected_permission_keys is not None:
        assert_ok(
            f"GET /auth/me ({label}) permission keys match",
            keys_ok,
            "set mismatch" if not keys_ok else "ok",
        )
    for field in (
        "id",
        "full_name",
        "phone",
        "business_id",
        "business_name",
        "business_type_code",
    ):
        assert_ok(
            f"GET /auth/me ({label}) has {field}",
            field in body,
        )


def test_auth_me_roles_and_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    suffix = unique_suffix
    password = DEFAULT_PASSWORD
    manager_phone = f"361{suffix:07d}"
    cashier_phone = f"362{suffix:07d}"
    senior_phone = f"363{suffix:07d}"

    expected_owner = permission_keys_for_role("owner")
    expected_manager = permission_keys_for_role("manager")
    expected_cashier = permission_keys_for_role("cashier")
    senior_keys = {
        "sales.view",
        "sales.create",
        "sales.payments.view",
        "products.view",
    }

    tenant = register_owner(
        http_client,
        api_base,
        suffix,
        business_name=f"Fix2 {suffix}",
        owner_name="Fix2 Owner",
        phone_prefix="360",
    )
    owner_headers = tenant.owner_headers

    roles = http_client.get(f"{api_base}/roles", headers=owner_headers).json()
    manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
    cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")

    for phone, name, role_id in (
        (manager_phone, "Manager", manager_role_id),
        (cashier_phone, "Cashier", cashier_role_id),
    ):
        u = http_client.post(
            f"{api_base}/users",
            headers=owner_headers,
            json={
                "full_name": name,
                "phone": phone,
                "password": password,
                "role_ids": [role_id],
            },
        )
        assert_ok(f"create {name}", u.status_code == 201, f"HTTP {u.status_code}")

    senior_role = http_client.post(
        f"{api_base}/roles",
        headers=owner_headers,
        json={
            "name": "Senior Cashier",
            "description": "Custom role for FIX 2 test",
            "permission_keys": sorted(senior_keys),
        },
    )
    assert_ok(
        "create Senior Cashier role",
        senior_role.status_code == 201,
        f"HTTP {senior_role.status_code}",
    )
    senior_role_id = senior_role.json()["id"]

    su = http_client.post(
        f"{api_base}/users",
        headers=owner_headers,
        json={
            "full_name": "Senior Cashier User",
            "phone": senior_phone,
            "password": password,
            "role_ids": [senior_role_id],
        },
    )
    assert_ok(
        "create Senior Cashier user",
        su.status_code == 201,
        f"HTTP {su.status_code}",
    )

    _check_me(
        http_client,
        api_base,
        owner_headers,
        label="owner",
        expected_roles=["owner"],
        expected_permission_count=68,
        expected_permission_keys=set(expected_owner),
    )

    manager_headers = login(
        http_client,
        api_base,
        manager_phone,
        password,
        label=f"login {manager_phone}",
    )
    _check_me(
        http_client,
        api_base,
        manager_headers,
        label="manager",
        expected_roles=["manager"],
        expected_permission_count=60,
        expected_permission_keys=set(expected_manager),
    )

    cashier_headers = login(
        http_client,
        api_base,
        cashier_phone,
        password,
        label=f"login {cashier_phone}",
    )
    _check_me(
        http_client,
        api_base,
        cashier_headers,
        label="cashier",
        expected_roles=["cashier"],
        expected_permission_count=16,
        expected_permission_keys=set(expected_cashier),
    )

    senior_headers = login(
        http_client,
        api_base,
        senior_phone,
        password,
        label=f"login {senior_phone}",
    )
    _check_me(
        http_client,
        api_base,
        senior_headers,
        label="Senior Cashier",
        expected_roles=["Senior Cashier"],
        expected_permission_count=len(senior_keys),
        expected_permission_keys=senior_keys,
    )
