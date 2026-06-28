#!/usr/bin/env python3
"""FIX 2: GET /auth/me includes roles and permission_keys."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.role_permission_seed import permission_keys_for_role  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f": {detail}"
    print(line)


def login(client: httpx.Client, phone: str, password: str) -> dict[str, str]:
    resp = client.post(
        f"{BASE}/auth/login",
        json={"phone": phone, "password": password},
    )
    record(f"login {phone}", resp.status_code == 200, f"HTTP {resp.status_code}")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def check_me(
    client: httpx.Client,
    headers: dict[str, str],
    *,
    label: str,
    expected_roles: list[str],
    expected_permission_count: int,
    expected_permission_keys: set[str] | None = None,
) -> None:
    resp = client.get(f"{BASE}/auth/me", headers=headers)
    if resp.status_code != 200:
        record(f"GET /auth/me ({label})", False, f"HTTP {resp.status_code}")
        return
    body = resp.json()
    roles = body.get("roles", [])
    keys = body.get("permission_keys", [])
    roles_ok = roles == expected_roles
    count_ok = len(keys) == expected_permission_count
    keys_ok = True
    if expected_permission_keys is not None:
        keys_ok = set(keys) == expected_permission_keys
    record(
        f"GET /auth/me ({label}) roles",
        roles_ok,
        f"roles={roles!r} expected={expected_roles!r}",
    )
    record(
        f"GET /auth/me ({label}) permission count",
        count_ok,
        f"count={len(keys)} expected={expected_permission_count}",
    )
    if expected_permission_keys is not None:
        record(
            f"GET /auth/me ({label}) permission keys match",
            keys_ok,
            "set mismatch" if not keys_ok else "ok",
        )
    # Legacy fields still present
    for field in (
        "id",
        "full_name",
        "phone",
        "business_id",
        "business_name",
        "business_type_code",
    ):
        record(
            f"GET /auth/me ({label}) has {field}",
            field in body,
            "",
        )


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    password = "TestPass1"
    owner_phone = f"360{suffix:07d}"
    manager_phone = f"361{suffix:07d}"
    cashier_phone = f"362{suffix:07d}"
    senior_phone = f"363{suffix:07d}"

    expected_owner = permission_keys_for_role("owner")
    expected_manager = permission_keys_for_role("manager")
    expected_cashier = permission_keys_for_role("cashier")
    senior_keys = {
        "sales.view",
        "sales.create",
        "sales.payments.view",
        "products.view",
    }

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Fix2 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Fix2 Owner",
                "owner_phone": owner_phone,
                "owner_password": password,
                "branch_name": "Main",
            },
        )
        record("register owner", reg.status_code == 201, f"HTTP {reg.status_code}")
        if reg.status_code != 201:
            return 1

        owner_headers = {
            "Authorization": f"Bearer {reg.json()['access_token']}"
        }

        roles = client.get(f"{BASE}/roles", headers=owner_headers).json()
        manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
        cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")

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
            record(f"create {name}", u.status_code == 201, f"HTTP {u.status_code}")

        senior_role = client.post(
            f"{BASE}/roles",
            headers=owner_headers,
            json={
                "name": "Senior Cashier",
                "description": "Custom role for FIX 2 test",
                "permission_keys": sorted(senior_keys),
            },
        )
        record(
            "create Senior Cashier role",
            senior_role.status_code == 201,
            f"HTTP {senior_role.status_code}",
        )
        senior_role_id = senior_role.json()["id"]

        su = client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": "Senior Cashier User",
                "phone": senior_phone,
                "password": password,
                "role_ids": [senior_role_id],
            },
        )
        record("create Senior Cashier user", su.status_code == 201, f"HTTP {su.status_code}")

        check_me(
            client,
            owner_headers,
            label="owner",
            expected_roles=["owner"],
            expected_permission_count=67,
            expected_permission_keys=set(expected_owner),
        )

        manager_headers = login(client, manager_phone, password)
        check_me(
            client,
            manager_headers,
            label="manager",
            expected_roles=["manager"],
            expected_permission_count=59,
            expected_permission_keys=set(expected_manager),
        )

        cashier_headers = login(client, cashier_phone, password)
        check_me(
            client,
            cashier_headers,
            label="cashier",
            expected_roles=["cashier"],
            expected_permission_count=15,
            expected_permission_keys=set(expected_cashier),
        )

        senior_headers = login(client, senior_phone, password)
        check_me(
            client,
            senior_headers,
            label="Senior Cashier",
            expected_roles=["Senior Cashier"],
            expected_permission_count=len(senior_keys),
            expected_permission_keys=senior_keys,
        )

    failed = [r for r in RESULTS if not r[1]]
    print("\n=== FIX 2 AUTH/ME SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
