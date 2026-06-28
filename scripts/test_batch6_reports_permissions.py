#!/usr/bin/env python3
"""Batch 6 smoke test: analytics and invoice export permission enforcement."""

from __future__ import annotations

import sys
import uuid
from datetime import date, timedelta

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
    owner_phone = f"360{suffix:07d}"
    manager_phone = f"361{suffix:07d}"
    cashier_phone = f"362{suffix:07d}"
    today = date.today()
    week_ago = today - timedelta(days=7)
    date_q = f"date_from={week_ago.isoformat()}&date_to={today.isoformat()}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch6 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch6 Owner",
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
            check(f"create {name.lower()} user", u, 201)

        manager_headers = login(client, manager_phone, password)
        cashier_headers = login(client, cashier_phone, password)

        read_cases = [
            (
                "GET /analytics/dashboard (manager)",
                manager_headers,
                f"{BASE}/analytics/dashboard",
                200,
            ),
            (
                "GET /analytics/dashboard (cashier, denied)",
                cashier_headers,
                f"{BASE}/analytics/dashboard",
                403,
            ),
            (
                "GET /analytics/sales-summary (manager)",
                manager_headers,
                f"{BASE}/analytics/sales-summary?{date_q}",
                200,
            ),
            (
                "GET /analytics/sales-summary (cashier, denied)",
                cashier_headers,
                f"{BASE}/analytics/sales-summary?{date_q}",
                403,
            ),
            (
                "GET /analytics/stock-valuation (manager)",
                manager_headers,
                f"{BASE}/analytics/stock-valuation",
                200,
            ),
            (
                "GET /analytics/profit-loss (manager)",
                manager_headers,
                f"{BASE}/analytics/profit-loss?{date_q}",
                200,
            ),
            (
                "GET /analytics/customer-insights (manager)",
                manager_headers,
                f"{BASE}/analytics/customer-insights",
                200,
            ),
            (
                "GET /analytics/fraud-alerts (owner)",
                owner_headers,
                f"{BASE}/analytics/fraud-alerts?{date_q}",
                200,
            ),
            (
                "GET /analytics/fraud-alerts (manager, denied)",
                manager_headers,
                f"{BASE}/analytics/fraud-alerts?{date_q}",
                403,
            ),
            (
                "GET /invoice/export/sales (manager)",
                manager_headers,
                f"{BASE}/invoice/export/sales?{date_q}&format=csv",
                200,
            ),
            (
                "GET /invoice/export/sales (cashier, denied)",
                cashier_headers,
                f"{BASE}/invoice/export/sales?{date_q}&format=csv",
                403,
            ),
            (
                "GET /invoice/export/inventory (manager)",
                manager_headers,
                f"{BASE}/invoice/export/inventory?format=csv",
                200,
            ),
            (
                "GET /invoice/export/inventory (cashier, denied)",
                cashier_headers,
                f"{BASE}/invoice/export/inventory?format=csv",
                403,
            ),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 6 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
