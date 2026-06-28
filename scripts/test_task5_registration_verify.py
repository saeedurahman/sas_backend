#!/usr/bin/env python3
"""Task 5: end-to-end verification of permission seeding on brand-new registration."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.role_permission_seed import (  # noqa: E402
    CASHIER_PERMISSION_KEYS,
    STANDARD_ROLE_NAMES,
    permission_keys_for_role,
)

BASE = "http://127.0.0.1:8000/api/v1"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f": {detail}"
    print(line)


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    password = "TestPass1"
    owner_phone = f"399{suffix:07d}"
    manager_phone = f"398{suffix:07d}"
    cashier_phone = f"397{suffix:07d}"

    expected_owner = permission_keys_for_role("owner")
    expected_manager = permission_keys_for_role("manager")
    expected_cashier = permission_keys_for_role("cashier")

    record(
        "expected owner count",
        len(expected_owner) == 68,
        f"{len(expected_owner)} keys (67 granular + products.manage)",
    )
    record(
        "expected manager count",
        len(expected_manager) == 60,
        f"{len(expected_manager)} keys",
    )
    record(
        "expected cashier count",
        len(expected_cashier) == 16,
        f"{len(expected_cashier)} keys",
    )
    record(
        "cashier excludes shifts.view",
        "shifts.view" not in expected_cashier,
        "",
    )

    with httpx.Client(timeout=60.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Task5 Verify {suffix}",
                "business_type_code": "retail",
                "owner_name": "Task5 Owner",
                "owner_phone": owner_phone,
                "owner_password": password,
                "branch_name": "Main",
            },
        )
        record("POST /auth/register", reg.status_code == 201, f"HTTP {reg.status_code}")
        if reg.status_code != 201:
            return 1

        reg_body = reg.json()
        owner_headers = {"Authorization": f"Bearer {reg_body['access_token']}"}
        owner_me = reg_body["user"]
        record(
            "register response user is owner context",
            owner_me["phone"] == owner_phone,
            owner_me["phone"],
        )

        roles_resp = client.get(f"{BASE}/roles", headers=owner_headers)
        record("GET /roles after register", roles_resp.status_code == 200, "")
        if roles_resp.status_code != 200:
            return 1

        roles = roles_resp.json()
        role_names = sorted(r["name"].lower() for r in roles)
        record(
            "exactly 3 standard roles",
            len(roles) == 3 and role_names == sorted(STANDARD_ROLE_NAMES),
            f"got {role_names}",
        )

        by_name = {r["name"].lower(): r for r in roles}
        for role_name, expected_keys in (
            ("owner", expected_owner),
            ("manager", expected_manager),
            ("cashier", expected_cashier),
        ):
            role = by_name.get(role_name)
            if role is None:
                record(f"{role_name} permission set", False, "role missing")
                continue
            actual = set(role.get("permission_keys") or [])
            record(
                f"{role_name} permission count",
                len(actual) == len(expected_keys),
                f"got {len(actual)}, expected {len(expected_keys)}",
            )
            missing = expected_keys - actual
            extra = actual - expected_keys
            record(
                f"{role_name} permission keys match seed",
                not missing and not extra,
                f"missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}",
            )

        owners = client.get(
            f"{BASE}/users?role=owner", headers=owner_headers
        )
        record("GET /users?role=owner", owners.status_code == 200, "")
        if owners.status_code == 200:
            owner_users = owners.json()
            record(
                "registering user has owner role",
                any(u["phone"] == owner_phone for u in owner_users),
                f"{len(owner_users)} owner user(s)",
            )

        manager_role_id = by_name["manager"]["id"]
        cashier_role_id = by_name["cashier"]["id"]

        mgr_create = client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": "Task5 Manager",
                "phone": manager_phone,
                "password": password,
                "role_ids": [manager_role_id],
            },
        )
        record("POST /users manager", mgr_create.status_code == 201, "")

        cash_create = client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": "Task5 Cashier",
                "phone": cashier_phone,
                "password": password,
                "role_ids": [cashier_role_id],
            },
        )
        record("POST /users cashier", cash_create.status_code == 201, "")

        for phone, role_name in (
            (manager_phone, "manager"),
            (cashier_phone, "cashier"),
        ):
            login = client.post(
                f"{BASE}/auth/login",
                json={"phone": phone, "password": password},
            )
            record(f"login {role_name}", login.status_code == 200, "")
            if login.status_code != 200:
                continue
            headers = {
                "Authorization": f"Bearer {login.json()['access_token']}"
            }
            me = client.get(f"{BASE}/auth/me", headers=headers)
            record(
                f"GET /auth/me ({role_name})",
                me.status_code == 200 and me.json()["phone"] == phone,
                me.json().get("phone", "") if me.status_code == 200 else "",
            )
            role_users = client.get(
                f"{BASE}/users?role={role_name}",
                headers=owner_headers,
            )
            if role_users.status_code == 200:
                record(
                    f"GET /users?role={role_name} lists user",
                    any(u["phone"] == phone for u in role_users.json()),
                    "",
                )

        # Permission enforcement spot-check on this fresh tenant
        mgr_login = client.post(
            f"{BASE}/auth/login",
            json={"phone": manager_phone, "password": password},
        )
        cash_login = client.post(
            f"{BASE}/auth/login",
            json={"phone": cashier_phone, "password": password},
        )
        if mgr_login.status_code == 200 and cash_login.status_code == 200:
            mgr_h = {
                "Authorization": f"Bearer {mgr_login.json()['access_token']}"
            }
            cash_h = {
                "Authorization": f"Bearer {cash_login.json()['access_token']}"
            }
            record(
                "manager can GET /users (users.view)",
                client.get(f"{BASE}/users", headers=mgr_h).status_code == 200,
                "",
            )
            record(
                "cashier denied GET /users",
                client.get(f"{BASE}/users", headers=cash_h).status_code == 403,
                "",
            )
            record(
                "cashier can POST /sales (sales.create)",
                client.get(f"{BASE}/products", headers=cash_h).status_code == 200,
                "",
            )
            record(
                "cashier denied GET /shifts (no shifts.view)",
                client.get(f"{BASE}/shifts", headers=cash_h).status_code == 403,
                "",
            )

    failed = [r for r in RESULTS if not r[1]]
    print("\n=== TASK 5 REGISTRATION VERIFY ===")
    print(f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, Failed: {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
