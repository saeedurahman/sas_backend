#!/usr/bin/env python3
"""Batch 3 smoke test: sales, returns, customers, invoice read permissions."""

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
    owner_phone = f"330{suffix:07d}"
    manager_phone = f"331{suffix:07d}"
    cashier_phone = f"332{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Batch3 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Batch3 Owner",
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
        product_id = None
        if unit.status_code == 201:
            product = client.post(
                f"{BASE}/products",
                headers=owner_headers,
                json={
                    "name": f"Sale Product {suffix}",
                    "base_unit_id": unit.json()["id"],
                    "product_type": "standard",
                    "tracking_type": "none",
                },
            )
            check("owner setup product", product, 201)
            if product.status_code == 201:
                product_id = product.json()["id"]
                client.post(
                    f"{BASE}/adjustments",
                    headers=owner_headers,
                    json={
                        "branch_id": branch_id,
                        "reason": "opening_balance",
                        "lines": [
                            {
                                "product_id": product_id,
                                "qty_delta": "100",
                                "cost_per_unit": "10.00",
                            }
                        ],
                    },
                )

        read_cases = [
            ("GET /sales (cashier)", cashier_headers, f"{BASE}/sales", 200),
            ("GET /customers (cashier)", cashier_headers, f"{BASE}/customers", 200),
            (
                "GET /returns (manager)",
                manager_headers,
                f"{BASE}/returns",
                200,
            ),
            (
                "GET /returns (cashier, denied)",
                cashier_headers,
                f"{BASE}/returns",
                403,
            ),
        ]
        for name, headers, url, expected in read_cases:
            check(name, client.get(url, headers=headers), expected)

        cust = client.post(
            f"{BASE}/customers",
            headers=manager_headers,
            json={"name": f"Customer {suffix}"},
        )
        check("POST /customers (manager)", cust, 201)
        customer_id = cust.json()["id"] if cust.status_code == 201 else None

        check(
            "POST /customers (cashier)",
            client.post(
                f"{BASE}/customers",
                headers=cashier_headers,
                json={"name": f"Cashier Customer {suffix}"},
            ),
            201,
        )

        if customer_id:
            check(
                "GET /customers/{id}/ledger (manager)",
                client.get(
                    f"{BASE}/customers/{customer_id}/ledger",
                    headers=manager_headers,
                ),
                200,
            )
            check(
                "GET /customers/{id}/ledger (cashier, denied)",
                client.get(
                    f"{BASE}/customers/{customer_id}/ledger",
                    headers=cashier_headers,
                ),
                403,
            )
            check(
                "PUT /customers/{id} (manager)",
                client.put(
                    f"{BASE}/customers/{customer_id}",
                    headers=manager_headers,
                    json={"name": f"Customer Updated {suffix}"},
                ),
                200,
            )
            check(
                "PUT /customers/{id} (cashier, denied)",
                client.put(
                    f"{BASE}/customers/{customer_id}",
                    headers=cashier_headers,
                    json={"name": "Blocked"},
                ),
                403,
            )
            payment_body = {
                "amount": "50.00",
                "payment_method": "cash",
            }
            check(
                "POST /customers/{id}/payments (manager)",
                client.post(
                    f"{BASE}/customers/{customer_id}/payments",
                    headers=manager_headers,
                    json=payment_body,
                ),
                201,
            )
            check(
                "POST /customers/{id}/payments (cashier, denied)",
                client.post(
                    f"{BASE}/customers/{customer_id}/payments",
                    headers=cashier_headers,
                    json=payment_body,
                ),
                403,
            )

        sale_id = None
        if product_id:
            sale_body = {
                "branch_id": branch_id,
                "lines": [
                    {
                        "product_id": product_id,
                        "qty": "1",
                        "unit_price": "25.00",
                    }
                ],
                "payments": [
                    {
                        "payment_method": "cash",
                        "amount": "25.00",
                    }
                ],
            }
            sale_mgr = client.post(
                f"{BASE}/sales",
                headers=manager_headers,
                json=sale_body,
            )
            check("POST /sales (manager)", sale_mgr, 201)
            if sale_mgr.status_code == 201:
                sale_id = sale_mgr.json()["id"]

            check(
                "POST /sales (cashier)",
                client.post(
                    f"{BASE}/sales",
                    headers=cashier_headers,
                    json={
                        **sale_body,
                        "payments": [{"payment_method": "cash", "amount": "30.00"}],
                    },
                ),
                201,
            )

        if sale_id:
            check(
                "GET /invoice/{sale_id} (cashier)",
                client.get(f"{BASE}/invoice/{sale_id}", headers=cashier_headers),
                200,
            )
            check(
                "GET /invoice/{sale_id}/thermal (cashier)",
                client.get(
                    f"{BASE}/invoice/{sale_id}/thermal",
                    headers=cashier_headers,
                ),
                200,
            )

        if product_id:
            draft_sale_body = {
                "branch_id": branch_id,
                "lines": [
                    {
                        "product_id": product_id,
                        "qty": "1",
                        "unit_price": "15.00",
                    }
                ],
                "payments": [],
            }
            draft_for_cashier = client.post(
                f"{BASE}/sales",
                headers=manager_headers,
                json=draft_sale_body,
            )
            if draft_for_cashier.status_code == 201:
                draft_id = draft_for_cashier.json()["id"]
                check(
                    "PUT /sales/{id}/cancel (cashier, denied)",
                    client.put(
                        f"{BASE}/sales/{draft_id}/cancel",
                        headers=cashier_headers,
                    ),
                    403,
                )

            draft_for_manager = client.post(
                f"{BASE}/sales",
                headers=manager_headers,
                json=draft_sale_body,
            )
            if draft_for_manager.status_code == 201:
                check(
                    "PUT /sales/{id}/cancel (manager)",
                    client.put(
                        f"{BASE}/sales/{draft_for_manager.json()['id']}/cancel",
                        headers=manager_headers,
                    ),
                    200,
                )

    failed = [r for r in RESULTS if not r[3]]
    print("\n=== BATCH 3 SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
