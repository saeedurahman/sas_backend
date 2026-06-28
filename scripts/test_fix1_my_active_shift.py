#!/usr/bin/env python3
"""FIX 1: cashier can read own active shift via GET /shifts/my-active."""

from __future__ import annotations

import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f": {detail}"
    print(line)


def check_status(name: str, response: httpx.Response, expected: int) -> bool:
    ok = response.status_code == expected
    detail = f"HTTP {response.status_code} (expected {expected})"
    if not ok:
        try:
            detail += f" body={response.json()}"
        except Exception:
            detail += f" body={response.text[:200]}"
    record(name, ok, detail)
    return ok


def login(client: httpx.Client, phone: str, password: str) -> dict[str, str]:
    resp = client.post(
        f"{BASE}/auth/login",
        json={"phone": phone, "password": password},
    )
    check_status(f"login {phone}", resp, 200)
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    password = "TestPass1"
    owner_phone = f"350{suffix:07d}"
    cashier_a_phone = f"351{suffix:07d}"
    cashier_b_phone = f"352{suffix:07d}"

    with httpx.Client(timeout=30.0) as client:
        reg = client.post(
            f"{BASE}/auth/register",
            json={
                "business_name": f"Fix1 {suffix}",
                "business_type_code": "retail",
                "owner_name": "Fix1 Owner",
                "owner_phone": owner_phone,
                "owner_password": password,
                "branch_name": "Main",
            },
        )
        if not check_status("register owner", reg, 201):
            return 1

        reg_body = reg.json()
        owner_headers = {"Authorization": f"Bearer {reg_body['access_token']}"}
        branch_id = reg_body["user"]["branch_id"]

        roles = client.get(f"{BASE}/roles", headers=owner_headers).json()
        cashier_role_id = next(
            r["id"] for r in roles if r["name"].lower() == "cashier"
        )

        for phone, name in (
            (cashier_a_phone, "Cashier A"),
            (cashier_b_phone, "Cashier B"),
        ):
            u = client.post(
                f"{BASE}/users",
                headers=owner_headers,
                json={
                    "full_name": name,
                    "phone": phone,
                    "password": password,
                    "role_ids": [cashier_role_id],
                },
            )
            check_status(f"create {name}", u, 201)

        cashier_a_headers = login(client, cashier_a_phone, password)
        cashier_b_headers = login(client, cashier_b_phone, password)

        register = client.post(
            f"{BASE}/registers",
            headers=owner_headers,
            json={
                "name": f"Register {suffix}",
                "branch_id": branch_id,
            },
        )
        if not check_status("POST /registers", register, 201):
            return 1
        register_id = register.json()["id"]

        open_shift = client.post(
            f"{BASE}/shifts/open",
            headers=cashier_a_headers,
            json={
                "cash_register_id": register_id,
                "opening_float": "100.00",
            },
        )
        if not check_status("POST /shifts/open (cashier A)", open_shift, 201):
            return 1
        shift_id = open_shift.json()["id"]

        my_active_a = client.get(
            f"{BASE}/shifts/my-active",
            headers=cashier_a_headers,
        )
        if not check_status("GET /shifts/my-active (cashier A, has shift)", my_active_a, 200):
            return 1
        body_a = my_active_a.json()
        record(
            "cashier A my-active returns open shift",
            body_a is not None
            and body_a.get("id") == shift_id
            and body_a.get("status") == "open"
            and body_a.get("opened_by") == open_shift.json()["opened_by"],
            f"id={body_a.get('id') if body_a else None}",
        )

        my_active_b = client.get(
            f"{BASE}/shifts/my-active",
            headers=cashier_b_headers,
        )
        if not check_status("GET /shifts/my-active (cashier B, no shift)", my_active_b, 200):
            return 1
        body_b = my_active_b.json()
        record(
            "cashier B my-active returns null",
            body_b is None,
            f"body={body_b!r}",
        )

        summary_denied = client.get(
            f"{BASE}/shifts/{shift_id}/summary",
            headers=cashier_a_headers,
        )
        check_status(
            "GET /shifts/{id}/summary (cashier A, still denied)",
            summary_denied,
            403,
        )

        list_denied = client.get(f"{BASE}/shifts", headers=cashier_a_headers)
        check_status(
            "GET /shifts (cashier A, still denied)",
            list_denied,
            403,
        )

    failed = [r for r in RESULTS if not r[1]]
    print("\n=== FIX 1 MY-ACTIVE SHIFT SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
