#!/usr/bin/env python3
"""FIX 3: verify cashier GET /discounts behavior (read-only diagnostic)."""

from __future__ import annotations

import json
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    password = "TestPass1"
    owner_phone = f"370{suffix:07d}"
    cashier_phone = f"371{suffix:07d}"

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
        print(f"register owner: HTTP {reg.status_code}")
        if reg.status_code != 201:
            return 1

        owner_headers = {
            "Authorization": f"Bearer {reg.json()['access_token']}"
        }
        roles = client.get(f"{BASE}/roles", headers=owner_headers).json()
        cashier_role_id = next(
            r["id"] for r in roles if r["name"].lower() == "cashier"
        )

        cu = client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": "Fix3 Cashier",
                "phone": cashier_phone,
                "password": password,
                "role_ids": [cashier_role_id],
            },
        )
        print(f"create cashier: HTTP {cu.status_code}")

        login = client.post(
            f"{BASE}/auth/login",
            json={"phone": cashier_phone, "password": password},
        )
        print(f"login cashier: HTTP {login.status_code}")
        cashier_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }

        me = client.get(f"{BASE}/auth/me", headers=cashier_headers)
        me_body = me.json()
        perm_keys = set(me_body.get("permission_keys", []))
        has_apply_discount = "sales.apply_discount" in perm_keys
        print(f"GET /auth/me cashier roles: {me_body.get('roles')}")
        print(
            f"GET /auth/me cashier has sales.apply_discount: {has_apply_discount}"
        )
        print(f"GET /auth/me cashier permission_keys count: {len(perm_keys)}")

        owner_discounts = client.post(
            f"{BASE}/discounts",
            headers=owner_headers,
            json={
                "name": f"Promo {suffix}",
                "discount_type": "percentage",
                "discount_value": "10",
            },
        )
        print(f"POST /discounts (owner setup): HTTP {owner_discounts.status_code}")
        scheme_id = None
        if owner_discounts.status_code == 201:
            scheme_id = owner_discounts.json()["id"]

        list_resp = client.get(f"{BASE}/discounts", headers=cashier_headers)
        print(f"\n=== CASHIER GET /discounts ===")
        print(f"HTTP {list_resp.status_code}")
        try:
            print(json.dumps(list_resp.json(), indent=2)[:500])
        except Exception:
            print(list_resp.text[:500])

        if scheme_id:
            detail_resp = client.get(
                f"{BASE}/discounts/{scheme_id}",
                headers=cashier_headers,
            )
            print(f"\n=== CASHIER GET /discounts/{{id}} ===")
            print(f"HTTP {detail_resp.status_code}")
            try:
                print(json.dumps(detail_resp.json(), indent=2)[:500])
            except Exception:
                print(detail_resp.text[:500])

        mgr_login = client.post(
            f"{BASE}/auth/login",
            json={"phone": owner_phone, "password": password},
        )
        # manager has apply_discount - use owner as control with manager
        roles_resp = client.get(f"{BASE}/roles", headers=owner_headers).json()
        manager_role_id = next(
            r["id"] for r in roles_resp if r["name"].lower() == "manager"
        )
        manager_phone = f"372{suffix:07d}"
        client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": "Fix3 Manager",
                "phone": manager_phone,
                "password": password,
                "role_ids": [manager_role_id],
            },
        )
        mgr_login = client.post(
            f"{BASE}/auth/login",
            json={"phone": manager_phone, "password": password},
        )
        mgr_headers = {
            "Authorization": f"Bearer {mgr_login.json()['access_token']}"
        }
        mgr_list = client.get(f"{BASE}/discounts", headers=mgr_headers)
        print(f"\n=== MANAGER GET /discounts (control) ===")
        print(f"HTTP {mgr_list.status_code}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
