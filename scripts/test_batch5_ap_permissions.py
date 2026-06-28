#!/usr/bin/env python3
"""Batch 5 smoke test: suppliers, supplier ledger, and expenses permissions."""

from __future__ import annotations

import sys
import uuid
from datetime import date

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
    owner_phone = f"350{suffix:07d}"
    manager_phone = f"351{suffix:07d}"
    cashier_phone = f"352{suffix:07d}"
    today = date.today().isoformat()

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch5 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch5 Owner",
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

        read_cases = [
            (
                "GET /suppliers (manager)",
                manager_headers,
                f"{BASE}/suppliers",
                200,
            ),
            (
                "GET /suppliers (cashier, denied)",
                cashier_headers,
                f"{BASE}/suppliers",
                403,
            ),
            (
                "GET /expenses (manager)",
                manager_headers,
                f"{BASE}/expenses",
                200,
            ),
            (
                "GET /expenses (cashier, denied)",
                cashier_headers,
                f"{BASE}/expenses",
                403,
            ),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        supplier = client.post(
            f"{BASE}/suppliers",
            headers=manager_headers,
            json={"name": f"Supplier {suffix}"},
        )
        check("POST /suppliers (manager)", supplier, 201)
        check(
            "POST /suppliers (cashier, denied)",
            client.post(
                f"{BASE}/suppliers",
                headers=cashier_headers,
                json={"name": f"Blocked {suffix}"},
            ),
            403,
        )
        supplier_id = supplier.json()["id"] if supplier.status_code == 201 else None

        if supplier_id:
            check(
                "GET /supplier-ledger/{id}/balance (manager)",
                client.get(
                    f"{BASE}/supplier-ledger/{supplier_id}/balance",
                    headers=manager_headers,
                ),
                200,
            )
            check(
                "GET /supplier-ledger/{id}/balance (cashier, denied)",
                client.get(
                    f"{BASE}/supplier-ledger/{supplier_id}/balance",
                    headers=cashier_headers,
                ),
                403,
            )
            check(
                "POST /supplier-ledger/{id}/payment (manager)",
                client.post(
                    f"{BASE}/supplier-ledger/{supplier_id}/payment",
                    headers=manager_headers,
                    json={
                        "amount": "25.00",
                        "payment_method": "cash",
                    },
                ),
                201,
            )
            check(
                "POST /supplier-ledger/{id}/payment (cashier, denied)",
                client.post(
                    f"{BASE}/supplier-ledger/{supplier_id}/payment",
                    headers=cashier_headers,
                    json={
                        "amount": "25.00",
                        "payment_method": "cash",
                    },
                ),
                403,
            )

        category = client.post(
            f"{BASE}/expenses/categories",
            headers=manager_headers,
            json={"name": f"Utilities {suffix}"},
        )
        check("POST /expenses/categories (manager)", category, 201)
        check(
            "POST /expenses/categories (cashier, denied)",
            client.post(
                f"{BASE}/expenses/categories",
                headers=cashier_headers,
                json={"name": f"Blocked Cat {suffix}"},
            ),
            403,
        )
        category_id = category.json()["id"] if category.status_code == 201 else None

        expense_id = None
        if category_id:
            expense = client.post(
                f"{BASE}/expenses",
                headers=manager_headers,
                json={
                    "branch_id": branch_id,
                    "expense_category_id": category_id,
                    "expense_date": today,
                    "amount": "100.00",
                    "payments": [
                        {
                            "payment_method": "cash",
                            "amount": "100.00",
                        }
                    ],
                },
            )
            check("POST /expenses (manager)", expense, 201)
            check(
                "POST /expenses (cashier, denied)",
                client.post(
                    f"{BASE}/expenses",
                    headers=cashier_headers,
                    json={
                        "branch_id": branch_id,
                        "expense_category_id": category_id,
                        "expense_date": today,
                        "amount": "50.00",
                    },
                ),
                403,
            )
            if expense.status_code == 201:
                expense_id = expense.json()["id"]

        if expense_id:
            check(
                "DELETE /expenses/{id} (manager, denied)",
                client.delete(
                    f"{BASE}/expenses/{expense_id}",
                    headers=manager_headers,
                ),
                403,
            )
            check(
                "DELETE /expenses/{id} (owner)",
                client.delete(
                    f"{BASE}/expenses/{expense_id}",
                    headers=owner_headers,
                ),
                200,
            )

        if supplier_id:
            supplier_del = client.post(
                f"{BASE}/suppliers",
                headers=manager_headers,
                json={"name": f"To Delete {suffix}"},
            )
            if supplier_del.status_code == 201:
                del_id = supplier_del.json()["id"]
                check(
                    "DELETE /suppliers/{id} (manager)",
                    client.delete(
                        f"{BASE}/suppliers/{del_id}",
                        headers=manager_headers,
                    ),
                    200,
                )
                check(
                    "DELETE /suppliers/{id} (cashier, denied)",
                    client.delete(
                        f"{BASE}/suppliers/{supplier_id}",
                        headers=cashier_headers,
                    ),
                    403,
                )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 5 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
