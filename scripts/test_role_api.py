#!/usr/bin/env python3
"""Manual integration test for Role Management API."""

from __future__ import annotations

import json
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
RESULTS: list[dict] = []


def record(name: str, response: httpx.Response, *, expect_status: int | None = None) -> dict:
    body: object
    try:
        body = response.json()
    except Exception:
        body = response.text[:500]
    ok = expect_status is None or response.status_code == expect_status
    entry = {
        "test": name,
        "status": response.status_code,
        "expected": expect_status,
        "pass": ok,
        "body": body,
    }
    RESULTS.append(entry)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: HTTP {response.status_code}" + (
        f" (expected {expect_status})" if expect_status else ""
    ))
    if not ok:
        print(json.dumps(body, indent=2)[:800])
    entry["body"] = body
    return entry


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    owner_phone = f"300{suffix:07d}"
    owner_password = "TestPass1"

    with httpx.Client(timeout=30.0) as client:
        # Register owner
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Role Test {suffix}",
                "business_type_code": "retail",
                "owner_name": "Role Test Owner",
                "owner_phone": owner_phone,
                "owner_password": owner_password,
                "branch_name": "Main",
            },
        )
        reg_body = record("register owner", reg, expect_status=201)
        if reg.status_code != 201:
            return 1
        owner_token = reg_body["body"]["access_token"]
        owner_headers = {"Authorization": f"Bearer {owner_token}"}

        # GET permissions
        record(
            "GET /permissions (owner)",
            client.get(f"{BASE}/permissions", headers=owner_headers),
            expect_status=200,
        )

        # GET roles
        roles_resp = client.get(f"{BASE}/roles", headers=owner_headers)
        roles_body = record("GET /roles (owner)", roles_resp, expect_status=200)
        roles = roles_body["body"]
        owner_role = next(r for r in roles if r["name"].lower() == "owner")
        manager_role = next(r for r in roles if r["name"].lower() == "manager")
        owner_role_id = owner_role["id"]
        manager_role_id = manager_role["id"]

        # POST custom role
        create_resp = client.post(
            f"{BASE}/roles",
            headers=owner_headers,
            json={
                "name": "Senior Cashier",
                "description": "Test custom role",
                "permission_keys": ["sales.view", "sales.create", "products.view"],
            },
        )
        create_body = record("POST /roles (custom)", create_resp, expect_status=201)
        if create_resp.status_code != 201:
            return 1
        custom_role_id = create_body["body"]["id"]

        # PUT permissions on custom role
        record(
            "PUT /roles/{id}/permissions (custom)",
            client.put(
                f"{BASE}/roles/{custom_role_id}/permissions",
                headers=owner_headers,
                json={
                    "permission_keys": [
                        "sales.view",
                        "sales.create",
                        "sales.payments.view",
                        "products.view",
                    ]
                },
            ),
            expect_status=200,
        )

        # Negative: rename owner
        record(
            "PUT /roles/{id} rename owner (negative)",
            client.put(
                f"{BASE}/roles/{owner_role_id}",
                headers=owner_headers,
                json={"name": "Super Owner"},
            ),
            expect_status=400,
        )

        # Negative: strip owner permission
        stripped_keys = [k for k in owner_role["permission_keys"] if k != "sales.view"]
        record(
            "PUT /roles/{id}/permissions strip owner (negative)",
            client.put(
                f"{BASE}/roles/{owner_role_id}/permissions",
                headers=owner_headers,
                json={"permission_keys": stripped_keys},
            ),
            expect_status=400,
        )

        # Negative: delete manager
        record(
            "DELETE /roles/{id} manager (negative)",
            client.delete(
                f"{BASE}/roles/{manager_role_id}",
                headers=owner_headers,
            ),
            expect_status=400,
        )

        # Negative: delete owner role (system, not user-count check)
        record(
            "DELETE /roles/{id} owner system (negative)",
            client.delete(
                f"{BASE}/roles/{owner_role_id}",
                headers=owner_headers,
            ),
            expect_status=400,
        )

        # Negative: delete custom role with assigned users
        assigned_role_resp = client.post(
            f"{BASE}/roles",
            headers=owner_headers,
            json={
                "name": "Assigned Role",
                "description": "Has a user",
                "permission_keys": ["sales.view"],
            },
        )
        assigned_role_body = record(
            "POST /roles Assigned Role",
            assigned_role_resp,
            expect_status=201,
        )
        assigned_role_id = assigned_role_body["body"]["id"]
        assigned_user_phone = f"302{suffix:07d}"
        au = client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": "Assigned User",
                "phone": assigned_user_phone,
                "password": owner_password,
                "role_ids": [assigned_role_id],
            },
        )
        record("POST /users for assigned role", au, expect_status=201)
        record(
            "DELETE /roles/{id} with assigned users (negative)",
            client.delete(
                f"{BASE}/roles/{assigned_role_id}",
                headers=owner_headers,
            ),
            expect_status=409,
        )

        # Create cashier user for 403 test
        roles_list = client.get(f"{BASE}/roles", headers=owner_headers).json()
        cashier_role = next(r for r in roles_list if r["name"].lower() == "cashier")
        cashier_phone = f"301{suffix:07d}"
        cu = client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": "Test Cashier",
                "phone": cashier_phone,
                "password": owner_password,
                "role_ids": [cashier_role["id"]],
            },
        )
        record("POST /users cashier", cu, expect_status=201)

        # Delete custom role with no users - should work
        record(
            "DELETE /roles/{id} custom (positive)",
            client.delete(
                f"{BASE}/roles/{custom_role_id}",
                headers=owner_headers,
            ),
            expect_status=200,
        )

        # Login as cashier
        login = client.post(
            f"{BASE}/auth/login",
            json={"phone": cashier_phone, "password": owner_password},
        )
        login_body = record("login cashier", login, expect_status=200)
        cashier_headers = {
            "Authorization": f"Bearer {login_body['body']['access_token']}"
        }

        record(
            "GET /roles (cashier)",
            client.get(f"{BASE}/roles", headers=cashier_headers),
            expect_status=200,
        )

        record(
            "POST /roles (cashier, negative)",
            client.post(
                f"{BASE}/roles",
                headers=cashier_headers,
                json={"name": "Blocked Role", "permission_keys": []},
            ),
            expect_status=403,
        )

    failed = [r for r in RESULTS if not r["pass"]]
    print("\n=== SUMMARY ===")
    print(f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, Failed: {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
