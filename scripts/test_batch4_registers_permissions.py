#!/usr/bin/env python3
"""Batch 4 smoke test: registers and shifts permission enforcement."""

from __future__ import annotations

import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
RESULTS: list[tuple[str, int, int, bool]] = []


def check(name: str, response: httpx.Response, expected: int) -> None:
    ok = response.status_code == expected
    RESULTS.append((name, response.status_code, expected, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: HTTP {response.status_code} (expected {expected})")
    if not ok:
        try:
            print(response.json())
        except Exception:
            print(response.text[:400])


def login(client: httpx.Client, phone: str, password: str) -> dict[str, str]:
    resp = client.post(
        f"{BASE}/auth/login",
        json={"phone": phone, "password": password},
    )
    check(f"login {phone}", resp, 200)
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    password = "TestPass1"
    owner_phone = f"340{suffix:07d}"
    manager_phone = f"341{suffix:07d}"
    cashier_phone = f"342{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch4 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch4 Owner",
                "owner_phone": owner_phone,
                "owner_password": password,
                "branch_name": "Main",
            },
        )
        check("register owner", reg, 201)
        if reg.status_code != 201:
            return 1

        reg_body = reg.json()
        owner_headers = {"Authorization": f"Bearer {reg_body['access_token']}"}
        branch_id = reg_body["user"]["branch_id"]

        roles = client.get(f"{BASE}/roles", headers=owner_headers).json()
        manager_role_id = next(
            r["id"] for r in roles if r["name"].lower() == "manager"
        )
        cashier_role_id = next(
            r["id"] for r in roles if r["name"].lower() == "cashier"
        )

        for phone, name, role_id in (
            (manager_phone, "Manager", manager_role_id),
            (cashier_phone, "Cashier", cashier_role_id),
        ):
            u = client.post(
                f"{BASE}/users",
                headers=owner_headers,
                json={
                    "full_name": name,
                    "phone": phone,
                    "password": password,
                    "role_ids": [role_id],
                },
            )
            check(f"create {name.lower()} user", u, 201)

        manager_headers = login(client, manager_phone, password)
        cashier_headers = login(client, cashier_phone, password)

        register = client.post(
            f"{BASE}/registers",
            headers=owner_headers,
            json={
                "name": f"Register {suffix}",
                "branch_id": branch_id,
            },
        )
        check("POST /registers (owner)", register, 201)
        register_id = register.json()["id"] if register.status_code == 201 else None

        register_mgr = client.post(
            f"{BASE}/registers",
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
            ("GET /registers (cashier)", cashier_headers, f"{BASE}/registers", 200),
            ("GET /registers (manager)", manager_headers, f"{BASE}/registers", 200),
            ("GET /shifts (manager)", manager_headers, f"{BASE}/shifts", 200),
            (
                "GET /shifts (cashier, denied)",
                cashier_headers,
                f"{BASE}/shifts",
                403,
            ),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        if register_id:
            check(
                "POST /registers (manager, denied)",
                client.post(
                    f"{BASE}/registers",
                    headers=manager_headers,
                    json={
                        "name": f"Blocked {suffix}",
                        "branch_id": branch_id,
                    },
                ),
                403,
            )
            check(
                "GET /registers/{id}/active-shift (manager)",
                client.get(
                    f"{BASE}/registers/{register_id}/active-shift",
                    headers=manager_headers,
                ),
                200,
            )
            check(
                "GET /registers/{id}/active-shift (cashier, denied)",
                client.get(
                    f"{BASE}/registers/{register_id}/active-shift",
                    headers=cashier_headers,
                ),
                403,
            )

            open_body = {
                "cash_register_id": register_id,
                "opening_float": "100.00",
            }
            open_cashier = client.post(
                f"{BASE}/shifts/open",
                headers=cashier_headers,
                json=open_body,
            )
            check("POST /shifts/open (cashier)", open_cashier, 201)
            shift_id = (
                open_cashier.json()["id"] if open_cashier.status_code == 201 else None
            )

            if register_mgr_id:
                check(
                    "POST /shifts/open (manager)",
                    client.post(
                        f"{BASE}/shifts/open",
                        headers=manager_headers,
                        json={
                            "cash_register_id": register_mgr_id,
                            "opening_float": "50.00",
                        },
                    ),
                    201,
                )

            if shift_id:
                check(
                    "POST /shifts/{id}/cash-movement (cashier)",
                    client.post(
                        f"{BASE}/shifts/{shift_id}/cash-movement",
                        headers=cashier_headers,
                        json={
                            "tx_type": "cash_in",
                            "amount": "10.00",
                            "notes": "test",
                        },
                    ),
                    201,
                )
                check(
                    "POST /shifts/{id}/close (cashier)",
                    client.post(
                        f"{BASE}/shifts/{shift_id}/close",
                        headers=cashier_headers,
                        json={"actual_cash": "110.00"},
                    ),
                    200,
                )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 4 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
