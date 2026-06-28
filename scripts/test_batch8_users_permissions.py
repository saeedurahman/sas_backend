#!/usr/bin/env python3
"""Batch 8 smoke test: users and roles permission enforcement."""

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
    owner_phone = f"380{suffix:07d}"
    manager_phone = f"381{suffix:07d}"
    cashier_phone = f"382{suffix:07d}"
    new_user_phone = f"383{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch8 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch8 Owner",
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
        owner_user_id = reg_body["user"]["id"]

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

        read_cases = [
            ("GET /users (manager)", manager_headers, f"{BASE}/users", 200),
            (
                "GET /users (cashier, denied)",
                cashier_headers,
                f"{BASE}/users",
                403,
            ),
            ("GET /roles (cashier)", cashier_headers, f"{BASE}/roles", 200),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        check(
            "POST /users (manager)",
            client.post(
                f"{BASE}/users",
                headers=manager_headers,
                json={
                    "full_name": "Mgr Created",
                    "phone": new_user_phone,
                    "password": password,
                    "role_ids": [cashier_role_id],
                },
            ),
            201,
        )
        check(
            "POST /users (cashier, denied)",
            client.post(
                f"{BASE}/users",
                headers=cashier_headers,
                json={
                    "full_name": "Blocked",
                    "phone": f"384{suffix:07d}",
                    "password": password,
                    "role_ids": [cashier_role_id],
                },
            ),
            403,
        )

        new_user = client.get(f"{BASE}/users", headers=manager_headers).json()
        target = next(
            (u for u in new_user if u["phone"] == new_user_phone),
            None,
        )
        if target:
            check(
                "PUT /users/{id} (manager)",
                client.put(
                    f"{BASE}/users/{target['id']}",
                    headers=manager_headers,
                    json={"full_name": "Mgr Created Updated"},
                ),
                200,
            )
            check(
                "DELETE /users/{id} (manager, denied)",
                client.delete(
                    f"{BASE}/users/{target['id']}",
                    headers=manager_headers,
                ),
                403,
            )
            check(
                "DELETE /users/{id} (owner)",
                client.delete(
                    f"{BASE}/users/{target['id']}",
                    headers=owner_headers,
                ),
                200,
            )

        me = client.get(f"{BASE}/auth/me", headers=cashier_headers)
        if me.status_code == 200:
            cashier_id = me.json()["id"]
            check(
                "PUT /users/{id}/pin self (cashier)",
                client.put(
                    f"{BASE}/users/{cashier_id}/pin",
                    headers=cashier_headers,
                    json={"pin_code": "1234"},
                ),
                200,
            )

        role_create = client.post(
            f"{BASE}/roles",
            headers=owner_headers,
            json={
                "name": f"Custom {suffix}",
                "description": "Batch8 test role",
                "permission_keys": ["sales.view"],
            },
        )
        check("POST /roles (owner)", role_create, 201)
        custom_role_id = (
            role_create.json()["id"] if role_create.status_code == 201 else None
        )

        check(
            "POST /roles (manager, denied)",
            client.post(
                f"{BASE}/roles",
                headers=manager_headers,
                json={
                    "name": f"Blocked {suffix}",
                    "description": "Should fail",
                    "permission_keys": ["sales.view"],
                },
            ),
            403,
        )
        check(
            "POST /roles (cashier, denied)",
            client.post(
                f"{BASE}/roles",
                headers=cashier_headers,
                json={
                    "name": f"Blocked2 {suffix}",
                    "description": "Should fail",
                    "permission_keys": [],
                },
            ),
            403,
        )

        if custom_role_id:
            check(
                "DELETE /roles/{id} (owner)",
                client.delete(
                    f"{BASE}/roles/{custom_role_id}",
                    headers=owner_headers,
                ),
                200,
            )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 8 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
