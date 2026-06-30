"""Tenant registration and POS setup helpers for integration tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.helpers.assertions import assert_status

DEFAULT_PASSWORD = "TestPass1"


@dataclass
class TenantContext:
    suffix: int
    password: str
    business_id: str
    branch_id: str
    owner_headers: dict[str, str]
    owner_phone: str
    manager_headers: dict[str, str] | None = None
    cashier_headers: dict[str, str] | None = None
    manager_phone: str | None = None
    cashier_phone: str | None = None
    unit_id: str | None = None
    product_id: str | None = None
    supplier_id: str | None = None
    register_id: str | None = None
    shift_id: str | None = None
    price_list_id: str | None = None
    _api_base: str = field(repr=False, default="")

    def url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        base = self._api_base.rstrip("/")
        suffix_path = path if path.startswith("/") else f"/{path}"
        return f"{base}{suffix_path}"


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(
    client: httpx.Client,
    api_base: str,
    phone: str,
    password: str,
    *,
    label: str = "",
) -> dict[str, str]:
    resp = client.post(
        f"{api_base}/auth/login",
        json={"phone": phone, "password": password},
    )
    assert_status(resp, 200, label=label or f"login {phone}")
    return auth_headers(resp.json()["access_token"])


def register_owner(
    client: httpx.Client,
    api_base: str,
    suffix: int,
    *,
    business_name: str,
    owner_name: str,
    phone_prefix: str = "300",
) -> TenantContext:
    password = DEFAULT_PASSWORD
    owner_phone = f"{phone_prefix}{suffix:07d}"
    resp = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": business_name,
            "business_type_code": "retail",
            "owner_name": owner_name,
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    assert_status(resp, 201, label="register owner")
    body = resp.json()
    return TenantContext(
        suffix=suffix,
        password=password,
        business_id=body["user"]["business_id"],
        branch_id=body["user"]["branch_id"],
        owner_headers=auth_headers(body["access_token"]),
        owner_phone=owner_phone,
        _api_base=api_base,
    )


def create_rbac_users(client: httpx.Client, tenant: TenantContext) -> TenantContext:
    roles = client.get(tenant.url("/roles"), headers=tenant.owner_headers).json()
    manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
    cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")

    owner_prefix = tenant.owner_phone[:3]
    manager_phone = f"{int(owner_prefix) + 1}{tenant.suffix:07d}"
    cashier_phone = f"{int(owner_prefix) + 2}{tenant.suffix:07d}"

    for phone, name, role_id in (
        (manager_phone, "Manager", manager_role_id),
        (cashier_phone, "Cashier", cashier_role_id),
    ):
        resp = client.post(
            tenant.url("/users"),
            headers=tenant.owner_headers,
            json={
                "full_name": name,
                "phone": phone,
                "password": tenant.password,
                "role_ids": [role_id],
            },
        )
        assert_status(resp, 201, label=f"create {name.lower()} user")

    tenant.manager_phone = manager_phone
    tenant.cashier_phone = cashier_phone
    tenant.manager_headers = login(
        client,
        tenant._api_base,
        manager_phone,
        tenant.password,
        label=f"login {manager_phone}",
    )
    tenant.cashier_headers = login(
        client,
        tenant._api_base,
        cashier_phone,
        tenant.password,
        label=f"login {cashier_phone}",
    )
    return tenant


def create_unit(
    client: httpx.Client,
    tenant: TenantContext,
    *,
    name: str = "Piece",
    symbol: str = "pc",
) -> str:
    resp = client.post(
        tenant.url("/units"),
        headers=tenant.owner_headers,
        json={"name": name, "symbol": symbol, "is_base_unit": True},
    )
    assert_status(resp, 201, label="create unit")
    tenant.unit_id = resp.json()["id"]
    return tenant.unit_id


def create_product(
    client: httpx.Client,
    tenant: TenantContext,
    *,
    name: str | None = None,
    unit_id: str | None = None,
) -> str:
    uid = unit_id or tenant.unit_id
    if uid is None:
        raise ValueError("unit_id required to create product")
    resp = client.post(
        tenant.url("/products"),
        headers=tenant.owner_headers,
        json={
            "name": name or f"Product {tenant.suffix}",
            "base_unit_id": uid,
            "product_type": "standard",
            "tracking_type": "none",
        },
    )
    assert_status(resp, 201, label="create product")
    tenant.product_id = resp.json()["id"]
    return tenant.product_id


def add_stock_adjustment(
    client: httpx.Client,
    tenant: TenantContext,
    *,
    product_id: str | None = None,
    qty_delta: str = "100",
    cost_per_unit: str = "50.00",
) -> None:
    pid = product_id or tenant.product_id
    if pid is None:
        raise ValueError("product_id required for stock adjustment")
    resp = client.post(
        tenant.url("/adjustments"),
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "reason": "opening_balance",
            "lines": [
                {
                    "product_id": pid,
                    "qty_delta": qty_delta,
                    "cost_per_unit": cost_per_unit,
                }
            ],
        },
    )
    assert_status(resp, 201, label="stock adjustment")


def open_register_shift(
    client: httpx.Client,
    tenant: TenantContext,
    *,
    register_name: str | None = None,
    opening_float: str = "500.00",
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    reg_resp = client.post(
        tenant.url("/registers"),
        headers=tenant.owner_headers,
        json={
            "name": register_name or f"Reg {tenant.suffix}",
            "branch_id": tenant.branch_id,
        },
    )
    assert_status(reg_resp, 201, label="create register")
    register_id = reg_resp.json()["id"]

    shift_headers = headers or tenant.manager_headers or tenant.owner_headers
    shift_resp = client.post(
        tenant.url("/shifts/open"),
        headers=shift_headers,
        json={
            "cash_register_id": register_id,
            "opening_float": opening_float,
        },
    )
    assert_status(shift_resp, 201, label="open shift")
    tenant.register_id = register_id
    tenant.shift_id = shift_resp.json()["id"]
    return register_id, tenant.shift_id


def build_rbac_tenant(
    client: httpx.Client,
    api_base: str,
    suffix: int,
    *,
    business_name: str,
    owner_name: str,
    phone_prefix: str = "300",
) -> TenantContext:
    tenant = register_owner(
        client,
        api_base,
        suffix,
        business_name=business_name,
        owner_name=owner_name,
        phone_prefix=phone_prefix,
    )
    return create_rbac_users(client, tenant)


def build_pos_tenant(
    client: httpx.Client,
    api_base: str,
    suffix: int,
    *,
    business_name: str = "POSTest",
    owner_name: str = "Owner",
    phone_prefix: str = "440",
    stock_qty: str = "100",
    cost_per_unit: str = "50.00",
) -> TenantContext:
    tenant = build_rbac_tenant(
        client,
        api_base,
        suffix,
        business_name=f"{business_name} {suffix}",
        owner_name=owner_name,
        phone_prefix=phone_prefix,
    )
    create_unit(client, tenant)
    create_product(client, tenant)
    open_register_shift(client, tenant)
    add_stock_adjustment(
        client,
        tenant,
        qty_delta=stock_qty,
        cost_per_unit=cost_per_unit,
    )
    return tenant


def build_void_race_tenant(
    client: httpx.Client,
    api_base: str,
    suffix: int,
) -> TenantContext:
    """Manager-only tenant for void/return concurrency (matches original script)."""
    password = DEFAULT_PASSWORD
    owner_phone = f"450{suffix:07d}"
    manager_phone = f"451{suffix:07d}"

    resp = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"VoidRace {suffix}",
            "business_type_code": "retail",
            "owner_name": "Race Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    assert_status(resp, 201, label="register owner")
    body = resp.json()
    tenant = TenantContext(
        suffix=suffix,
        password=password,
        business_id=body["user"]["business_id"],
        branch_id=body["user"]["branch_id"],
        owner_headers=auth_headers(body["access_token"]),
        owner_phone=owner_phone,
        _api_base=api_base,
    )

    roles = client.get(tenant.url("/roles"), headers=tenant.owner_headers).json()
    manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
    mgr_resp = client.post(
        tenant.url("/users"),
        headers=tenant.owner_headers,
        json={
            "full_name": "Manager",
            "phone": manager_phone,
            "password": password,
            "role_ids": [manager_role_id],
        },
    )
    assert_status(mgr_resp, 201, label="create manager user")
    tenant.manager_phone = manager_phone
    tenant.manager_headers = login(
        client,
        api_base,
        manager_phone,
        password,
        label=f"login {manager_phone}",
    )

    create_unit(client, tenant)
    create_product(client, tenant, name=f"Race Product {suffix}")
    open_register_shift(client, tenant)
    add_stock_adjustment(
        client,
        tenant,
        qty_delta="200",
        cost_per_unit="50.00",
    )
    return tenant


def build_cancel_race_tenant(
    client: httpx.Client,
    api_base: str,
    suffix: int,
) -> TenantContext:
    """Manager-only tenant for cancel vs return concurrency (matches original script)."""
    password = DEFAULT_PASSWORD
    owner_phone = f"460{suffix:07d}"
    manager_phone = f"461{suffix:07d}"

    resp = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"CancelRace {suffix}",
            "business_type_code": "retail",
            "owner_name": "Cancel Race Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    assert_status(resp, 201, label="register owner")
    body = resp.json()
    tenant = TenantContext(
        suffix=suffix,
        password=password,
        business_id=body["user"]["business_id"],
        branch_id=body["user"]["branch_id"],
        owner_headers=auth_headers(body["access_token"]),
        owner_phone=owner_phone,
        _api_base=api_base,
    )

    roles = client.get(tenant.url("/roles"), headers=tenant.owner_headers).json()
    manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
    mgr_resp = client.post(
        tenant.url("/users"),
        headers=tenant.owner_headers,
        json={
            "full_name": "Manager",
            "phone": manager_phone,
            "password": password,
            "role_ids": [manager_role_id],
        },
    )
    assert_status(mgr_resp, 201, label="create manager user")
    tenant.manager_phone = manager_phone
    tenant.manager_headers = login(
        client,
        api_base,
        manager_phone,
        password,
        label=f"login {manager_phone}",
    )

    create_unit(client, tenant)
    create_product(client, tenant, name=f"Cancel Race Product {suffix}")
    open_register_shift(client, tenant)
    add_stock_adjustment(
        client,
        tenant,
        qty_delta="200",
        cost_per_unit="50.00",
    )
    return tenant


def create_supplier(
    client: httpx.Client,
    tenant: TenantContext,
    *,
    name: str | None = None,
) -> str:
    resp = client.post(
        tenant.url("/suppliers"),
        headers=tenant.owner_headers,
        json={"name": name or f"Supplier {tenant.suffix}"},
    )
    assert_status(resp, 201, label="create supplier")
    tenant.supplier_id = resp.json()["id"]
    return tenant.supplier_id


def build_fifo_tenant(
    client: httpx.Client,
    api_base: str,
    suffix: int,
) -> TenantContext:
    """Owner tenant with unit, product, supplier for FIFO tests."""
    tenant = register_owner(
        client,
        api_base,
        suffix,
        business_name=f"FIFO {suffix}",
        owner_name="FIFO Owner",
        phone_prefix="420",
    )
    create_unit(client, tenant)
    create_product(client, tenant, name=f"FIFO Product {suffix}")
    create_supplier(client, tenant)
    return tenant
