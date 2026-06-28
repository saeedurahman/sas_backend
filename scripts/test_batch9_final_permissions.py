#!/usr/bin/env python3
"""Batch 9 smoke test: discounts, branches, business, audit, auth/logout."""

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


def login(client: httpx.Client, phone: str, password: str) -> dict:
    resp = client.post(
        f"{BASE}/auth/login",
        json={"phone": phone, "password": password},
    )
    check(f"login {phone}", resp, 200)
    body = resp.json()
    return {
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "refresh_token": body["refresh_token"],
    }


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    password = "TestPass1"
    owner_phone = f"390{suffix:07d}"
    manager_phone = f"391{suffix:07d}"
    cashier_phone = f"392{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        check(
            "GET /business/types (public)",
            client.get(f"{BASE}/business/types"),
            200,
        )

        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch9 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch9 Owner",
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
        owner_refresh = reg_body["refresh_token"]
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

        manager = login(client, manager_phone, password)
        cashier = login(client, cashier_phone, password)
        manager_headers = manager["headers"]
        cashier_headers = cashier["headers"]

        read_cases = [
            (
                "GET /discounts (manager)",
                manager_headers,
                f"{BASE}/discounts",
                200,
            ),
            (
                "GET /discounts (cashier)",
                cashier_headers,
                f"{BASE}/discounts",
                200,
            ),
            (
                "GET /branches (cashier)",
                cashier_headers,
                f"{BASE}/branches",
                200,
            ),
            (
                "GET /business/me (cashier)",
                cashier_headers,
                f"{BASE}/business/me",
                200,
            ),
            (
                "GET /audit (owner)",
                owner_headers,
                f"{BASE}/audit",
                200,
            ),
            (
                "GET /audit (manager, denied)",
                manager_headers,
                f"{BASE}/audit",
                403,
            ),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        check(
            "POST /discounts (owner)",
            client.post(
                f"{BASE}/discounts",
                headers=owner_headers,
                json={
                    "name": f"Scheme {suffix}",
                    "discount_type": "percentage",
                    "discount_value": "10",
                },
            ),
            201,
        )
        check(
            "POST /discounts (manager, denied)",
            client.post(
                f"{BASE}/discounts",
                headers=manager_headers,
                json={
                    "name": f"Blocked {suffix}",
                    "discount_type": "percentage",
                    "discount_value": "5",
                },
            ),
            403,
        )

        check(
            "PUT /branches/{id} (manager)",
            client.put(
                f"{BASE}/branches/{branch_id}",
                headers=manager_headers,
                json={"name": f"Main Updated {suffix}"},
            ),
            200,
        )
        check(
            "PUT /branches/{id} (cashier, denied)",
            client.put(
                f"{BASE}/branches/{branch_id}",
                headers=cashier_headers,
                json={"name": "Blocked"},
            ),
            403,
        )

        check(
            "PUT /business/me (manager, denied)",
            client.put(
                f"{BASE}/business/me",
                headers=manager_headers,
                json={"name": "Blocked Business"},
            ),
            403,
        )

        check(
            "POST /auth/logout (cashier)",
            client.post(
                f"{BASE}/auth/logout",
                headers=cashier_headers,
                json={"refresh_token": cashier["refresh_token"]},
            ),
            200,
        )
        check(
            "POST /auth/logout (owner)",
            client.post(
                f"{BASE}/auth/logout",
                headers=owner_headers,
                json={"refresh_token": owner_refresh},
            ),
            200,
        )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 9 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
