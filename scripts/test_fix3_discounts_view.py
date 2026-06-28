#!/usr/bin/env python3
"""FIX 3: discounts.view — cashier can read schemes; writes stay owner-only."""

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
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    password = "TestPass1"
    owner_phone = f"380{suffix:07d}"
    manager_phone = f"381{suffix:07d}"
    cashier_phone = f"382{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Fix3 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Fix3 Owner",
                "owner_phone": owner_phone,
                "owner_password": password,
                "branch_name": "Main",
            },
        )
        check("register owner", reg, 201)
        if reg.status_code != 201:
            return 1

        owner_headers = {
            "Authorization": f"Bearer {reg.json()['access_token']}"
        }
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
            check(f"create {name}", u, 201)

        manager_headers = login(client, manager_phone, password)
        cashier_headers = login(client, cashier_phone, password)

        create = client.post(
            f"{BASE}/discounts",
            headers=owner_headers,
            json={
                "name": f"Promo {suffix}",
                "discount_type": "percentage",
                "discount_value": "10",
            },
        )
        check("POST /discounts (owner setup)", create, 201)
        scheme_id = create.json()["id"] if create.status_code == 201 else None

        check(
            "GET /discounts (owner)",
            client.get(f"{BASE}/discounts", headers=owner_headers),
            200,
        )
        check(
            "GET /discounts (manager)",
            client.get(f"{BASE}/discounts", headers=manager_headers),
            200,
        )
        check(
            "GET /discounts (cashier)",
            client.get(f"{BASE}/discounts", headers=cashier_headers),
            200,
        )

        if scheme_id:
            check(
                "GET /discounts/{id} (cashier)",
                client.get(
                    f"{BASE}/discounts/{scheme_id}",
                    headers=cashier_headers,
                ),
                200,
            )

        me = client.get(f"{BASE}/auth/me", headers=cashier_headers)
        has_key = (
            me.status_code == 200
            and "discounts.view" in me.json().get("permission_keys", [])
        )
        RESULTS.append(
            ("cashier /auth/me has discounts.view", 200 if has_key else 0, 200, has_key)
        )
        mark = "PASS" if has_key else "FAIL"
        print(
            f"[{mark}] cashier /auth/me has discounts.view: "
            f"{'found' if has_key else 'missing'}"
        )

        check(
            "POST /discounts (manager, denied)",
            client.post(
                f"{BASE}/discounts",
                headers=manager_headers,
                json={
                    "name": "Blocked",
                    "discount_type": "percentage",
                    "discount_value": "5",
                },
            ),
            403,
        )
        check(
            "POST /discounts (cashier, denied)",
            client.post(
                f"{BASE}/discounts",
                headers=cashier_headers,
                json={
                    "name": "Blocked",
                    "discount_type": "percentage",
                    "discount_value": "5",
                },
            ),
            403,
        )

        if scheme_id:
            check(
                "PUT /discounts/{id} (manager, denied)",
                client.put(
                    f"{BASE}/discounts/{scheme_id}",
                    headers=manager_headers,
                    json={"discount_value": "15"},
                ),
                403,
            )
            check(
                "PUT /discounts/{id} (cashier, denied)",
                client.put(
                    f"{BASE}/discounts/{scheme_id}",
                    headers=cashier_headers,
                    json={"discount_value": "15"},
                ),
                403,
            )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== FIX 3 DISCOUNTS.VIEW SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
