#!/usr/bin/env python3
"""Batch 1 smoke test: products module permission enforcement."""

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
    owner_phone = f"310{suffix:07d}"
    manager_phone = f"311{suffix:07d}"
    cashier_phone = f"312{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch1 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch1 Owner",
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

        unit_resp = client.post(
            f"{BASE}/units",
            headers=owner_headers,
            json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
        )
        check("owner setup unit", unit_resp, 201)
        unit_id = unit_resp.json()["id"] if unit_resp.status_code == 201 else None

        read_cases = [
            ("GET /categories (owner)", owner_headers, f"{BASE}/categories", 200),
            ("GET /categories (manager)", manager_headers, f"{BASE}/categories", 200),
            ("GET /categories (cashier)", cashier_headers, f"{BASE}/categories", 200),
            ("GET /brands (owner)", owner_headers, f"{BASE}/brands", 200),
            ("GET /units (cashier)", cashier_headers, f"{BASE}/units", 200),
            ("GET /products (cashier)", cashier_headers, f"{BASE}/products", 200),
            ("GET /prices/lists (cashier)", cashier_headers, f"{BASE}/prices/lists", 200),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        write_allow = [
            (
                "POST /categories (owner)",
                owner_headers,
                f"{BASE}/categories",
                {"name": f"Cat {suffix}"},
                201,
            ),
            (
                "POST /categories (manager)",
                manager_headers,
                f"{BASE}/categories",
                {"name": f"Cat M {suffix}"},
                201,
            ),
            (
                "POST /categories (cashier, denied)",
                cashier_headers,
                f"{BASE}/categories",
                {"name": f"Cat C {suffix}"},
                403,
            ),
            (
                "POST /prices/lists (owner)",
                owner_headers,
                f"{BASE}/prices/lists",
                {"name": f"Retail {suffix}", "list_type": "retail"},
                201,
            ),
            (
                "POST /prices/lists (manager)",
                manager_headers,
                f"{BASE}/prices/lists",
                {"name": f"Wholesale {suffix}", "list_type": "wholesale"},
                201,
            ),
            (
                "POST /prices/lists (cashier, denied)",
                cashier_headers,
                f"{BASE}/prices/lists",
                {"name": f"Blocked {suffix}", "list_type": "retail"},
                403,
            ),
        ]
        for name, headers, url, body, expected in write_allow:
            check(name, client.post(url, headers=headers, json=body), expected)

        if unit_id:
            product_body = {
                "name": f"Product {suffix}",
                "base_unit_id": unit_id,
                "product_type": "standard",
                "tracking_type": "none",
            }
            create_owner = client.post(
                f"{BASE}/products",
                headers=owner_headers,
                json=product_body,
            )
            check("POST /products (owner)", create_owner, 201)
            product_id = (
                create_owner.json()["id"]
                if create_owner.status_code == 201
                else None
            )

            check(
                "POST /products (manager)",
                client.post(
                    f"{BASE}/products",
                    headers=manager_headers,
                    json={
                        **product_body,
                        "name": f"Product M {suffix}",
                        "sku": f"SKU-M-{suffix}",
                    },
                ),
                201,
            )
            check(
                "POST /products (cashier, denied)",
                client.post(
                    f"{BASE}/products",
                    headers=cashier_headers,
                    json={
                        **product_body,
                        "name": f"Product C {suffix}",
                        "sku": f"SKU-C-{suffix}",
                    },
                ),
                403,
            )

            if product_id:
                check(
                    "DELETE /products (manager, denied)",
                    client.delete(
                        f"{BASE}/products/{product_id}",
                        headers=manager_headers,
                    ),
                    403,
                )
                check(
                    "DELETE /products (cashier, denied)",
                    client.delete(
                        f"{BASE}/products/{product_id}",
                        headers=cashier_headers,
                    ),
                    403,
                )
                check(
                    "DELETE /products (owner)",
                    client.delete(
                        f"{BASE}/products/{product_id}",
                        headers=owner_headers,
                    ),
                    200,
                )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 1 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
