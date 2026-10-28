"""Cashier my-active shift access (migrated from test_fix1_my_active_shift)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.records import assert_ok
from tests.helpers.tenants import DEFAULT_PASSWORD, register_owner

pytestmark = pytest.mark.integration


def test_my_active_shift_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    suffix = unique_suffix
    password = DEFAULT_PASSWORD
    cashier_a_phone = f"351{suffix:07d}"
    cashier_b_phone = f"352{suffix:07d}"

    tenant = register_owner(
        http_client,
        api_base,
        suffix,
        business_name=f"Fix1 {suffix}",
        owner_name="Fix1 Owner",
        phone_prefix="350",
    )
    owner_headers = tenant.owner_headers
    branch_id = tenant.branch_id

    roles = http_client.get(f"{api_base}/roles", headers=owner_headers).json()
    cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")

    for phone, name in (
        (cashier_a_phone, "Cashier A"),
        (cashier_b_phone, "Cashier B"),
    ):
        u = http_client.post(
            f"{api_base}/users",
            headers=owner_headers,
            json={
                "full_name": name,
                "phone": phone,
                "password": password,
                "role_ids": [cashier_role_id],
            },
        )
        assert_status(u, 201, label=f"create {name}")

    login_a = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": cashier_a_phone, "password": password},
    )
    assert_status(login_a, 200, label=f"login {cashier_a_phone}")
    cashier_a_headers = {"Authorization": f"Bearer {login_a.json()['access_token']}"}

    login_b = http_client.post(
        f"{api_base}/auth/login",
        json={"phone": cashier_b_phone, "password": password},
    )
    assert_status(login_b, 200, label=f"login {cashier_b_phone}")
    cashier_b_headers = {"Authorization": f"Bearer {login_b.json()['access_token']}"}

    register = http_client.post(
        f"{api_base}/registers",
        headers=owner_headers,
        json={
            "name": f"Register {suffix}",
            "branch_id": branch_id,
        },
    )
    assert_status(register, 201, label="POST /registers")
    register_id = register.json()["id"]

    open_shift = http_client.post(
        f"{api_base}/shifts/open",
        headers=cashier_a_headers,
        json={
            "cash_register_id": register_id,
            "opening_float": "100.00",
        },
    )
    assert_status(open_shift, 201, label="POST /shifts/open (cashier A)")
    shift_id = open_shift.json()["id"]

    my_active_a = http_client.get(
        f"{api_base}/shifts/my-active",
        headers=cashier_a_headers,
    )
    assert_status(
        my_active_a,
        200,
        label="GET /shifts/my-active (cashier A, has shift)",
    )
    body_a = my_active_a.json()
    assert_ok(
        "cashier A my-active returns open shift",
        body_a is not None
        and body_a.get("id") == shift_id
        and body_a.get("status") == "open"
        and body_a.get("opened_by") == open_shift.json()["opened_by"],
        f"id={body_a.get('id') if body_a else None}",
    )

    my_active_b = http_client.get(
        f"{api_base}/shifts/my-active",
        headers=cashier_b_headers,
    )
    assert_status(
        my_active_b,
        200,
        label="GET /shifts/my-active (cashier B, no shift)",
    )
    body_b = my_active_b.json()
    assert_ok(
        "cashier B my-active returns null",
        body_b is None,
        f"body={body_b!r}",
    )

    summary_denied = http_client.get(
        f"{api_base}/shifts/{shift_id}/summary",
        headers=cashier_a_headers,
    )
    assert_status(
        summary_denied,
        403,
        label="GET /shifts/{id}/summary (cashier A, still denied)",
    )

    list_denied = http_client.get(f"{api_base}/shifts", headers=cashier_a_headers)
    assert_status(
        list_denied,
        403,
        label="GET /shifts (cashier A, still denied)",
    )
