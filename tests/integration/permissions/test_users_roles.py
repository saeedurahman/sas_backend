"""Users and roles permission enforcement (migrated from test_batch8)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import DEFAULT_PASSWORD, build_rbac_tenant

pytestmark = pytest.mark.integration


def test_users_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch8 {unique_suffix}",
        owner_name="Batch8 Owner",
        phone_prefix="380",
    )
    suffix = tenant.suffix
    password = DEFAULT_PASSWORD
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    new_user_phone = f"383{suffix:07d}"
    assert manager_headers is not None and cashier_headers is not None

    roles = http_client.get(f"{api_base}/roles", headers=owner_headers).json()
    cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")

    read_cases = [
        ("GET /users (manager)", manager_headers, f"{api_base}/users", 200),
        (
            "GET /users (cashier, denied)",
            cashier_headers,
            f"{api_base}/users",
            403,
        ),
        ("GET /roles (cashier)", cashier_headers, f"{api_base}/roles", 200),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    assert_status(
        http_client.post(
            f"{api_base}/users",
            headers=manager_headers,
            json={
                "full_name": "Mgr Created",
                "phone": new_user_phone,
                "password": password,
                "role_ids": [cashier_role_id],
            },
        ),
        201,
        label="POST /users (manager)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/users",
            headers=cashier_headers,
            json={
                "full_name": "Blocked",
                "phone": f"384{suffix:07d}",
                "password": password,
                "role_ids": [cashier_role_id],
            },
        ),
        403,
        label="POST /users (cashier, denied)",
    )

    new_user = http_client.get(f"{api_base}/users", headers=manager_headers).json()
    target = next(
        (u for u in new_user if u["phone"] == new_user_phone),
        None,
    )
    if target:
        assert_status(
            http_client.put(
                f"{api_base}/users/{target['id']}",
                headers=manager_headers,
                json={"full_name": "Mgr Created Updated"},
            ),
            200,
            label="PUT /users/{id} (manager)",
        )
        assert_status(
            http_client.delete(
                f"{api_base}/users/{target['id']}",
                headers=manager_headers,
            ),
            403,
            label="DELETE /users/{id} (manager, denied)",
        )
        assert_status(
            http_client.delete(
                f"{api_base}/users/{target['id']}",
                headers=owner_headers,
            ),
            200,
            label="DELETE /users/{id} (owner)",
        )

    me = http_client.get(f"{api_base}/auth/me", headers=cashier_headers)
    if me.status_code == 200:
        cashier_id = me.json()["id"]
        assert_status(
            http_client.put(
                f"{api_base}/users/{cashier_id}/pin",
                headers=cashier_headers,
                json={"pin_code": "1234"},
            ),
            200,
            label="PUT /users/{id}/pin self (cashier)",
        )

    role_create = http_client.post(
        f"{api_base}/roles",
        headers=owner_headers,
        json={
            "name": f"Custom {suffix}",
            "description": "Batch8 test role",
            "permission_keys": ["sales.view"],
        },
    )
    assert_status(role_create, 201, label="POST /roles (owner)")
    custom_role_id = (
        role_create.json()["id"] if role_create.status_code == 201 else None
    )

    assert_status(
        http_client.post(
            f"{api_base}/roles",
            headers=manager_headers,
            json={
                "name": f"Blocked {suffix}",
                "description": "Should fail",
                "permission_keys": ["sales.view"],
            },
        ),
        403,
        label="POST /roles (manager, denied)",
    )
    assert_status(
        http_client.post(
            f"{api_base}/roles",
            headers=cashier_headers,
            json={
                "name": f"Blocked2 {suffix}",
                "description": "Should fail",
                "permission_keys": [],
            },
        ),
        403,
        label="POST /roles (cashier, denied)",
    )

    if custom_role_id:
        assert_status(
            http_client.delete(
                f"{api_base}/roles/{custom_role_id}",
                headers=owner_headers,
            ),
            200,
            label="DELETE /roles/{id} (owner)",
        )


def test_put_user_roles(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    from tests.helpers.records import assert_ok
    from tests.helpers.tenants import login

    suffix = unique_suffix
    password = DEFAULT_PASSWORD
    owner_phone = f"400{suffix:07d}"
    manager_phone = f"401{suffix:07d}"
    cashier_phone = f"402{suffix:07d}"
    other_owner_phone = f"403{suffix:07d}"

    reg = http_client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"UserRoles {suffix}",
            "business_type_code": "retail",
            "owner_name": "Roles Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    assert_status(reg, 201, label="register owner")
    if reg.status_code != 201:
        return

    owner_headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    owner_id = reg.json()["user"]["id"]

    roles = http_client.get(f"{api_base}/roles", headers=owner_headers).json()
    manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
    cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")
    owner_role_id = next(r["id"] for r in roles if r["name"].lower() == "owner")

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
        assert_status(u, 201, label=f"create {name}")

    cashier_user = next(
        u
        for u in http_client.get(f"{api_base}/users", headers=owner_headers).json()
        if u["phone"] == cashier_phone
    )
    cashier_id = cashier_user["id"]

    promote = http_client.put(
        f"{api_base}/users/{cashier_id}/roles",
        headers=owner_headers,
        json={"role_ids": [manager_role_id]},
    )
    assert_status(
        promote,
        200,
        label="PUT /users/{id}/roles promote cashier to manager",
    )
    if promote.status_code == 200:
        body = promote.json()
        roles_ok = body.get("role_ids") == [manager_role_id] or body.get(
            "role_ids"
        ) == [str(manager_role_id)]
        assert_ok(
            "response role_ids updated",
            roles_ok,
            f"role_ids={body.get('role_ids')!r}",
        )

    mgr_list = http_client.get(
        f"{api_base}/users?role=manager",
        headers=owner_headers,
    )
    if mgr_list.status_code == 200:
        listed = any(u["id"] == cashier_id for u in mgr_list.json())
        assert_ok("cashier listed under manager role", listed)

    cashier_headers = login(
        http_client,
        api_base,
        cashier_phone,
        password,
        label=f"login {cashier_phone}",
    )
    me = http_client.get(f"{api_base}/auth/me", headers=cashier_headers)
    if me.status_code == 200:
        roles_me = me.json().get("roles", [])
        mgr_ok = roles_me == ["manager"]
        assert_ok(
            "promoted user /auth/me roles",
            mgr_ok,
            f"roles={roles_me!r}",
        )

    strip_owner = http_client.put(
        f"{api_base}/users/{owner_id}/roles",
        headers=owner_headers,
        json={"role_ids": [manager_role_id]},
    )
    assert_status(
        strip_owner,
        400,
        label="PUT /users/{id}/roles remove last owner (blocked)",
    )

    other_reg = http_client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Other {suffix}",
            "business_type_code": "retail",
            "owner_name": "Other Owner",
            "owner_phone": other_owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    assert_status(other_reg, 201, label="register other business")
    other_role_id = None
    if other_reg.status_code == 201:
        other_headers = {
            "Authorization": f"Bearer {other_reg.json()['access_token']}"
        }
        other_roles = http_client.get(f"{api_base}/roles", headers=other_headers).json()
        other_role_id = next(
            r["id"] for r in other_roles if r["name"].lower() == "manager"
        )

    if other_role_id:
        cross = http_client.put(
            f"{api_base}/users/{cashier_id}/roles",
            headers=owner_headers,
            json={"role_ids": [other_role_id]},
        )
        assert_status(
            cross,
            400,
            label="PUT /users/{id}/roles foreign business role (blocked)",
        )

    manager_headers = login(
        http_client,
        api_base,
        manager_phone,
        password,
        label=f"login {manager_phone}",
    )
    assert_status(
        http_client.put(
            f"{api_base}/users/{cashier_id}/roles",
            headers=manager_headers,
            json={"role_ids": [cashier_role_id]},
        ),
        403,
        label="PUT /users/{id}/roles (manager denied)",
    )

    bogus = http_client.put(
        f"{api_base}/users/{cashier_id}/roles",
        headers=owner_headers,
        json={"role_ids": [str(uuid.uuid4())]},
    )
    assert_status(
        bogus,
        400,
        label="PUT /users/{id}/roles unknown role_id (blocked)",
    )

    restore = http_client.put(
        f"{api_base}/users/{owner_id}/roles",
        headers=owner_headers,
        json={"role_ids": [owner_role_id]},
    )
    assert_status(restore, 200, label="PUT /users/{id}/roles restore owner role")


def test_role_management_api(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    suffix = unique_suffix
    owner_phone = f"300{suffix:07d}"
    owner_password = DEFAULT_PASSWORD

    reg = http_client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Role Test {suffix}",
            "business_type_code": "retail",
            "owner_name": "Role Test Owner",
            "owner_phone": owner_phone,
            "owner_password": owner_password,
            "branch_name": "Main",
        },
    )
    assert_status(reg, 201, label="register owner")
    if reg.status_code != 201:
        return
    owner_headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    assert_status(
        http_client.get(f"{api_base}/permissions", headers=owner_headers),
        200,
        label="GET /permissions (owner)",
    )

    roles_resp = http_client.get(f"{api_base}/roles", headers=owner_headers)
    assert_status(roles_resp, 200, label="GET /roles (owner)")
    roles = roles_resp.json()
    owner_role = next(r for r in roles if r["name"].lower() == "owner")
    manager_role = next(r for r in roles if r["name"].lower() == "manager")
    owner_role_id = owner_role["id"]
    manager_role_id = manager_role["id"]

    create_resp = http_client.post(
        f"{api_base}/roles",
        headers=owner_headers,
        json={
            "name": "Senior Cashier",
            "description": "Test custom role",
            "permission_keys": ["sales.view", "sales.create", "products.view"],
        },
    )
    assert_status(create_resp, 201, label="POST /roles (custom)")
    if create_resp.status_code != 201:
        return
    custom_role_id = create_resp.json()["id"]

    assert_status(
        http_client.put(
            f"{api_base}/roles/{custom_role_id}/permissions",
            headers=owner_headers,
            json={
                "permission_keys": [
                    "sales.view",
                    "sales.create",
                    "sales.payments.view",
                    "products.view",
                ]
            },
        ),
        200,
        label="PUT /roles/{id}/permissions (custom)",
    )

    assert_status(
        http_client.put(
            f"{api_base}/roles/{owner_role_id}",
            headers=owner_headers,
            json={"name": "Super Owner"},
        ),
        400,
        label="PUT /roles/{id} rename owner (negative)",
    )

    stripped_keys = [k for k in owner_role["permission_keys"] if k != "sales.view"]
    assert_status(
        http_client.put(
            f"{api_base}/roles/{owner_role_id}/permissions",
            headers=owner_headers,
            json={"permission_keys": stripped_keys},
        ),
        400,
        label="PUT /roles/{id}/permissions strip owner (negative)",
    )

    assert_status(
        http_client.delete(
            f"{api_base}/roles/{manager_role_id}",
            headers=owner_headers,
        ),
        400,
        label="DELETE /roles/{id} manager (negative)",
    )

    assert_status(
        http_client.delete(
            f"{api_base}/roles/{owner_role_id}",
            headers=owner_headers,
        ),
        400,
        label="DELETE /roles/{id} owner system (negative)",
    )

    assigned_role_resp = http_client.post(
        f"{api_base}/roles",
        headers=owner_headers,
        json={
            "name": "Assigned Role",
            "description": "Has a user",
            "permission_keys": ["sales.view"],
        },
    )
    assert_status(assigned_role_resp, 201, label="POST /roles Assigned Role")
    assigned_role_id = assigned_role_resp.json()["id"]
    assigned_user_phone = f"302{suffix:07d}"
    au = http_client.post(
        f"{api_base}/users",
        headers=owner_headers,
        json={
            "full_name": "Assigned User",
            "phone": assigned_user_phone,
            "password": owner_password,
            "role_ids": [assigned_role_id],
        },
    )
    assert_status(au, 201, label="POST /users for assigned role")
    assert_status(
        http_client.delete(
            f"{api_base}/roles/{assigned_role_id}",
            headers=owner_headers,
        ),
        409,
        label="DELETE /roles/{id} with assigned users (negative)",
    )

    roles_list = http_client.get(f"{api_base}/roles", headers=owner_headers).json()
    cashier_role = next(r for r in roles_list if r["name"].lower() == "cashier")
    cashier_phone = f"301{suffix:07d}"
    cu = http_client.post(
        f"{api_base}/users",
        headers=owner_headers,
        json={
            "full_name": "Test Cashier",
            "phone": cashier_phone,
            "password": owner_password,
            "role_ids": [cashier_role["id"]],
        },
    )
    assert_status(cu, 201, label="POST /users cashier")

    assert_status(
        http_client.delete(
            f"{api_base}/roles/{custom_role_id}",
            headers=owner_headers,
        ),
        200,
        label="DELETE /roles/{id} custom (positive)",
    )

    login = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": cashier_phone, "password": owner_password},
    )
    assert_status(login, 200, label="login cashier")
    cashier_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert_status(
        http_client.get(f"{api_base}/roles", headers=cashier_headers),
        200,
        label="GET /roles (cashier)",
    )

    assert_status(
        http_client.post(
            f"{api_base}/roles",
            headers=cashier_headers,
            json={"name": "Blocked Role", "permission_keys": []},
        ),
        403,
        label="POST /roles (cashier, negative)",
    )
