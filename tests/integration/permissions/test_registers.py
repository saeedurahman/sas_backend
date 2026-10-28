"""Registers and shifts permission enforcement (migrated from test_batch4)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant

pytestmark = pytest.mark.integration


def test_registers_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch4 {unique_suffix}",
        owner_name="Batch4 Owner",
        phone_prefix="340",
    )
    suffix = tenant.suffix
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    branch_id = tenant.branch_id
    assert manager_headers is not None and cashier_headers is not None

    register = http_client.post(
        f"{api_base}/registers",
        headers=owner_headers,
        json={
            "name": f"Register {suffix}",
            "branch_id": branch_id,
        },
    )
    assert_status(register, 201, label="POST /registers (owner)")
    register_id = register.json()["id"] if register.status_code == 201 else None

    register_mgr = http_client.post(
        f"{api_base}/registers",
        headers=owner_headers,
        json={
            "name": f"Register Mgr {suffix}",
            "branch_id": branch_id,
        },
    )
    register_mgr_id = (
        register_mgr.json()["id"] if register_mgr.status_code == 201 else None
    )

    read_cases = [
        ("GET /registers (cashier)", cashier_headers, f"{api_base}/registers", 200),
        ("GET /registers (manager)", manager_headers, f"{api_base}/registers", 200),
        ("GET /shifts (manager)", manager_headers, f"{api_base}/shifts", 200),
        (
            "GET /shifts (cashier, denied)",
            cashier_headers,
            f"{api_base}/shifts",
            403,
        ),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    if register_id:
        assert_status(
            http_client.post(
                f"{api_base}/registers",
                headers=manager_headers,
                json={
                    "name": f"Blocked {suffix}",
                    "branch_id": branch_id,
                },
            ),
            403,
            label="POST /registers (manager, denied)",
        )
        assert_status(
            http_client.get(
                f"{api_base}/registers/{register_id}/active-shift",
                headers=manager_headers,
            ),
            200,
            label="GET /registers/{id}/active-shift (manager)",
        )
        assert_status(
            http_client.get(
                f"{api_base}/registers/{register_id}/active-shift",
                headers=cashier_headers,
            ),
            403,
            label="GET /registers/{id}/active-shift (cashier, denied)",
        )

        open_body = {
            "cash_register_id": register_id,
            "opening_float": "100.00",
        }
        open_cashier = http_client.post(
            f"{api_base}/shifts/open",
            headers=cashier_headers,
            json=open_body,
        )
        assert_status(open_cashier, 201, label="POST /shifts/open (cashier)")
        shift_id = (
            open_cashier.json()["id"] if open_cashier.status_code == 201 else None
        )

        if register_mgr_id:
            assert_status(
                http_client.post(
                    f"{api_base}/shifts/open",
                    headers=manager_headers,
                    json={
                        "cash_register_id": register_mgr_id,
                        "opening_float": "50.00",
                    },
                ),
                201,
                label="POST /shifts/open (manager)",
            )

        if shift_id:
            assert_status(
                http_client.post(
                    f"{api_base}/shifts/{shift_id}/cash-movement",
                    headers=cashier_headers,
                    json={
                        "tx_type": "cash_in",
                        "amount": "10.00",
                        "notes": "test",
                    },
                ),
                201,
                label="POST /shifts/{id}/cash-movement (cashier)",
            )
            assert_status(
                http_client.post(
                    f"{api_base}/shifts/{shift_id}/close",
                    headers=cashier_headers,
                    json={"actual_cash": "110.00"},
                ),
                200,
                label="POST /shifts/{id}/close (cashier)",
            )
