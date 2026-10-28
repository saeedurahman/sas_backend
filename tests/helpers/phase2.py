"""Small builders shared by the phase-2 (money / inventory correctness) tests."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import TenantContext, auth_headers, login


def D(value: Any) -> Decimal:
    return Decimal(str(value))


def sale_body(
    tenant: TenantContext,
    *,
    product_id: str | None = None,
    qty: str = "1",
    unit_price: str = "100.00",
    payments: list[dict] | None = None,
    **extra: Any,
) -> dict:
    if payments is None:
        payments = [{"payment_method": "cash", "amount": str(D(qty) * D(unit_price))}]
    line = {"product_id": product_id or tenant.product_id, "qty": qty, "unit_price": unit_price}
    line.update(extra.pop("line_extra", {}))
    body = {"branch_id": tenant.branch_id, "lines": [line], "payments": payments}
    body.update(extra)
    return body


def post_sale(
    client: httpx.Client,
    tenant: TenantContext,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    return client.post(
        tenant.url("/sales"),
        headers=headers or tenant.owner_headers,
        json=sale_body(tenant, **kwargs),
    )


def create_customer(
    client: httpx.Client, tenant: TenantContext, *, credit_limit: str = "0"
) -> str:
    resp = client.post(
        tenant.url("/customers"),
        headers=tenant.owner_headers,
        json={"name": f"Cust {uuid.uuid4().hex[:6]}", "credit_limit": credit_limit},
    )
    assert_status(resp, 201, label="create customer")
    return resp.json()["id"]


def customer_balance(client: httpx.Client, tenant: TenantContext, customer_id: str) -> Decimal:
    from tests.helpers.db import db_scalar

    return D(
        db_scalar(
            "SELECT COALESCE(SUM(amount), 0) FROM customer_ledger "
            "WHERE customer_id = :c AND deleted_at IS NULL",
            {"c": customer_id},
        )
    )


def stock_qty(tenant: TenantContext, product_id: str | None = None, branch_id: str | None = None) -> Decimal:
    from tests.helpers.db import db_scalar

    return D(
        db_scalar(
            "SELECT COALESCE(SUM(qty), 0) FROM stock_movements WHERE business_id = :b "
            "AND branch_id = :br AND product_id = :p AND deleted_at IS NULL",
            {
                "b": tenant.business_id,
                "br": branch_id or tenant.branch_id,
                "p": product_id or tenant.product_id,
            },
        )
    )


def open_shift_for(
    client: httpx.Client,
    tenant: TenantContext,
    headers: dict[str, str],
    *,
    branch_id: str | None = None,
    name: str | None = None,
) -> tuple[str, str]:
    """Create a fresh register at ``branch_id`` and open a shift on it as ``headers``."""
    reg = client.post(
        tenant.url("/registers"),
        headers=tenant.owner_headers,
        json={"name": name or f"R{uuid.uuid4().hex[:6]}", "branch_id": branch_id or tenant.branch_id},
    )
    assert_status(reg, 201, label="create register")
    shift = client.post(
        tenant.url("/shifts/open"),
        headers=headers,
        json={"cash_register_id": reg.json()["id"], "opening_float": "0"},
    )
    assert_status(shift, 201, label="open shift")
    return reg.json()["id"], shift.json()["id"]


def new_product(client: httpx.Client, tenant: TenantContext, name: str | None = None) -> str:
    resp = client.post(
        tenant.url("/products"),
        headers=tenant.owner_headers,
        json={
            "name": name or f"P{uuid.uuid4().hex[:6]}",
            "base_unit_id": tenant.unit_id,
            "product_type": "standard",
            "tracking_type": "none",
        },
    )
    assert_status(resp, 201, label="create product")
    return resp.json()["id"]


def stock_in(
    client: httpx.Client,
    tenant: TenantContext,
    product_id: str,
    qty: str,
    cost: str = "50.00",
    *,
    branch_id: str | None = None,
) -> None:
    resp = client.post(
        tenant.url("/adjustments"),
        headers=tenant.owner_headers,
        json={
            "branch_id": branch_id or tenant.branch_id,
            "reason": "opening_balance",
            "lines": [{"product_id": product_id, "qty_delta": qty, "cost_per_unit": cost}],
        },
    )
    assert_status(resp, 201, label="stock in")


def second_branch(client: httpx.Client, tenant: TenantContext) -> str:
    """Another branch in the tenant. Inserted directly: the trial plan caps the API at one."""
    from tests.helpers.db import db_execute, db_scalar

    new_id = str(uuid.uuid4())
    db_execute(
        """
        INSERT INTO branches (id, business_id, name, is_head_office, is_active, created_at, updated_at)
        VALUES (:id, :b, :n, FALSE, TRUE, NOW(), NOW())
        """,
        {"id": new_id, "b": tenant.business_id, "n": f"Branch {uuid.uuid4().hex[:5]}"},
    )
    assert db_scalar("SELECT COUNT(*) FROM branches WHERE id = :i", {"i": new_id}) == 1
    return new_id


def add_user(
    client: httpx.Client,
    tenant: TenantContext,
    role_name: str,
    *,
    branch_id: str | None = None,
) -> dict[str, str]:
    roles = client.get(tenant.url("/roles"), headers=tenant.owner_headers).json()
    role_id = next(r["id"] for r in roles if r["name"].lower() == role_name)
    phone = f"6{uuid.uuid4().int % 10**9:09d}"
    body = {
        "full_name": f"{role_name.title()} {phone[-4:]}",
        "phone": phone,
        "password": tenant.password,
        "role_ids": [role_id],
    }
    if branch_id:
        body["branch_id"] = branch_id
    assert_status(
        client.post(tenant.url("/users"), headers=tenant.owner_headers, json=body),
        201,
        label="create user",
    )
    return login(client, tenant._api_base, phone, tenant.password)


__all__ = [n for n in dir() if not n.startswith("_")] + ["auth_headers"]
