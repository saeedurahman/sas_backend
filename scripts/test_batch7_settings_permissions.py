#!/usr/bin/env python3
"""Batch 7 smoke test: settings, tax rates, notifications, search permissions."""

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
    owner_phone = f"370{suffix:07d}"
    manager_phone = f"371{suffix:07d}"
    cashier_phone = f"372{suffix:07d}"
    no_search_phone = f"373{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch7 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch7 Owner",
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

        no_search_role = client.post(
            f"{BASE}/roles",
            headers=owner_headers,
            json={
                "name": "No Search",
                "description": "Auth only for search denial test",
                "permission_keys": ["auth.logout"],
            },
        )
        check("create no-search role", no_search_role, 201)
        no_search_role_id = (
            no_search_role.json()["id"] if no_search_role.status_code == 201 else None
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

        if no_search_role_id:
            ns = client.post(
                f"{BASE}/users",
                headers=owner_headers,
                json={
                    "full_name": "No Search User",
                    "phone": no_search_phone,
                    "password": password,
                    "role_ids": [no_search_role_id],
                },
            )
            check("create no-search user", ns, 201)

        manager_headers = login(client, manager_phone, password)
        cashier_headers = login(client, cashier_phone, password)
        no_search_headers = (
            login(client, no_search_phone, password)
            if no_search_role_id
            else {}
        )

        read_cases = [
            (
                "GET /settings (manager)",
                manager_headers,
                f"{BASE}/settings",
                200,
            ),
            (
                "GET /settings (cashier, denied)",
                cashier_headers,
                f"{BASE}/settings",
                403,
            ),
            (
                "GET /tax-rates (manager)",
                manager_headers,
                f"{BASE}/tax-rates",
                200,
            ),
            (
                "GET /tax-rates (cashier, denied)",
                cashier_headers,
                f"{BASE}/tax-rates",
                403,
            ),
            (
                "GET /notifications (cashier)",
                cashier_headers,
                f"{BASE}/notifications",
                200,
            ),
            (
                "GET /search?q=test (cashier)",
                cashier_headers,
                f"{BASE}/search?q=test",
                200,
            ),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        if no_search_headers:
            check(
                "GET /search?q=test (no catalog perms, denied)",
                client.get(f"{BASE}/search?q=test", headers=no_search_headers),
                403,
            )

        check(
            "PUT /settings (owner)",
            client.put(
                f"{BASE}/settings",
                headers=owner_headers,
                json={
                    "setting_key": f"batch7_{suffix}",
                    "setting_value": {"enabled": True},
                },
            ),
            200,
        )
        check(
            "PUT /settings (manager, denied)",
            client.put(
                f"{BASE}/settings",
                headers=manager_headers,
                json={
                    "setting_key": f"blocked_{suffix}",
                    "setting_value": {"enabled": False},
                },
            ),
            403,
        )

        tax = client.post(
            f"{BASE}/tax-rates",
            headers=owner_headers,
            json={"name": f"GST {suffix}", "rate": "17"},
        )
        check("POST /tax-rates (owner)", tax, 201)
        check(
            "POST /tax-rates (manager, denied)",
            client.post(
                f"{BASE}/tax-rates",
                headers=manager_headers,
                json={"name": f"Blocked {suffix}", "rate": "5"},
            ),
            403,
        )

        check(
            "POST /notifications/check-alerts (owner)",
            client.post(
                f"{BASE}/notifications/check-alerts",
                headers=owner_headers,
            ),
            200,
        )
        check(
            "POST /notifications/check-alerts (cashier, denied)",
            client.post(
                f"{BASE}/notifications/check-alerts",
                headers=cashier_headers,
            ),
            403,
        )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 7 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
