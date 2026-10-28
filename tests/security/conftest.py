"""Fixtures for the security-hardening suite (each boots its own server process)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.helpers.server import managed_server
from tests.helpers.tenants import TenantContext, create_rbac_users, register_owner


@pytest.fixture(scope="session")
def sec_api() -> str:
    """Server with relaxed rate limits and MAX_LOGIN_ATTEMPTS=3."""
    with managed_server() as api_base:
        yield api_base


@pytest.fixture
def client() -> httpx.Client:
    with httpx.Client(timeout=60.0) as c:
        yield c


def _suffix() -> int:
    return uuid.uuid4().int % 10_000_000


@pytest.fixture
def tenant(client: httpx.Client, sec_api: str) -> TenantContext:
    """Owner + manager + cashier."""
    ctx = register_owner(
        client,
        sec_api,
        _suffix(),
        business_name=f"SecTest {uuid.uuid4().hex[:8]}",
        owner_name="Owner",
        phone_prefix="710",
    )
    return create_rbac_users(client, ctx)


def login_raw(client: httpx.Client, api: str, phone: str, password: str, **extra):
    return client.post(
        f"{api}/auth/login", json={"phone": phone, "password": password, **extra}
    )


def slug_of(client: httpx.Client, tenant: TenantContext) -> str:
    me = client.get(tenant.url("/auth/me"), headers=tenant.owner_headers)
    return me.json()["business_slug"]
