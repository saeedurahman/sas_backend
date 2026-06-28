#!/usr/bin/env python3
"""Batch 2 smoke test: inventory module permission enforcement."""

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
    owner_phone = f"320{suffix:07d}"
    manager_phone = f"321{suffix:07d}"
    cashier_phone = f"322{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch2 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch2 Owner",
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

        unit = client.post(
            f"{BASE}/units",
            headers=owner_headers,
            json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
        )
        check("owner setup unit", unit, 201)
        unit_id = unit.json()["id"] if unit.status_code == 201 else None

        product_id = None
        if unit_id:
            product = client.post(
                f"{BASE}/products",
                headers=owner_headers,
                json={
                    "name": f"Inv Product {suffix}",
                    "base_unit_id": unit_id,
                    "product_type": "standard",
                    "tracking_type": "none",
                },
            )
            check("owner setup product", product, 201)
            if product.status_code == 201:
                product_id = product.json()["id"]

        supplier_id = None
        supplier = client.post(
            f"{BASE}/suppliers",
            headers=owner_headers,
            json={"name": f"Supplier {suffix}"},
        )
        check("owner setup supplier", supplier, 201)
        if supplier.status_code == 201:
            supplier_id = supplier.json()["id"]

        stock_q = f"branch_id={branch_id}"
        read_cases = [
            (
                "GET /stock/movements (owner)",
                owner_headers,
                f"{BASE}/stock/movements?{stock_q}",
                200,
            ),
            (
                "GET /stock/movements (manager)",
                manager_headers,
                f"{BASE}/stock/movements?{stock_q}",
                200,
            ),
            (
                "GET /stock/movements (cashier)",
                cashier_headers,
                f"{BASE}/stock/movements?{stock_q}",
                200,
            ),
            (
                "GET /adjustments (cashier)",
                cashier_headers,
                f"{BASE}/adjustments",
                200,
            ),
            (
                "GET /purchases/orders (manager)",
                manager_headers,
                f"{BASE}/purchases/orders",
                200,
            ),
            (
                "GET /purchases/orders (cashier, denied)",
                cashier_headers,
                f"{BASE}/purchases/orders",
                403,
            ),
            (
                "GET /transfers (manager)",
                manager_headers,
                f"{BASE}/transfers",
                200,
            ),
            (
                "GET /transfers (cashier, denied)",
                cashier_headers,
                f"{BASE}/transfers",
                403,
            ),
            (
                "GET /waste (manager)",
                manager_headers,
                f"{BASE}/waste",
                200,
            ),
            (
                "GET /waste (cashier, denied)",
                cashier_headers,
                f"{BASE}/waste",
                403,
            ),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        if product_id and supplier_id:
            adj_body = {
                "branch_id": branch_id,
                "reason": "opening_balance",
                "lines": [
                    {
                        "product_id": product_id,
                        "qty_delta": "10",
                        "cost_per_unit": "5.00",
                    }
                ],
            }
            check(
                "POST /adjustments (manager)",
                client.post(
                    f"{BASE}/adjustments",
                    headers=manager_headers,
                    json=adj_body,
                ),
                201,
            )
            check(
                "POST /adjustments (cashier, denied)",
                client.post(
                    f"{BASE}/adjustments",
                    headers=cashier_headers,
                    json=adj_body,
                ),
                403,
            )

            po_body = {
                "supplier_id": supplier_id,
                "branch_id": branch_id,
                "lines": [
                    {
                        "product_id": product_id,
                        "ordered_qty": "5",
                        "cost_per_unit": "4.00",
                    }
                ],
            }
            check(
                "POST /purchases/orders (manager)",
                client.post(
                    f"{BASE}/purchases/orders",
                    headers=manager_headers,
                    json=po_body,
                ),
                201,
            )
            check(
                "POST /purchases/orders (cashier, denied)",
                client.post(
                    f"{BASE}/purchases/orders",
                    headers=cashier_headers,
                    json=po_body,
                ),
                403,
            )

            waste_body = {
                "branch_id": branch_id,
                "reason": "damage",
                "lines": [
                    {
                        "product_id": product_id,
                        "qty": "1",
                        "cost_per_unit": "5.00",
                    }
                ],
            }
            check(
                "POST /waste (manager)",
                client.post(
                    f"{BASE}/waste",
                    headers=manager_headers,
                    json=waste_body,
                ),
                201,
            )
            check(
                "POST /waste (cashier, denied)",
                client.post(
                    f"{BASE}/waste",
                    headers=cashier_headers,
                    json=waste_body,
                ),
                403,
            )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 2 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
