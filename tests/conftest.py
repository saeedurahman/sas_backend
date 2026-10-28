"""Shared pytest fixtures for backend integration tests."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from tests.helpers.tenants import TenantContext, build_pos_tenant, build_rbac_tenant


@pytest.fixture(scope="session")
def api_base() -> str:
    return os.environ.get("API_BASE", "http://127.0.0.1:8000/api/v1")


@pytest.fixture(scope="session")
def require_server(api_base: str) -> None:
    health_url = api_base.replace("/api/v1", "") + "/health"
    try:
        response = httpx.get(health_url, timeout=3.0)
    except httpx.ConnectError:
        pytest.skip(
            f"API server not running at {health_url}. "
            "Start with: uvicorn main:app --reload --port 8000"
        )
    if response.status_code != 200:
        pytest.fail(f"Server unhealthy: {health_url} → HTTP {response.status_code}")


@pytest.fixture
def unique_suffix() -> int:
    return uuid.uuid4().int % 10_000_000


@pytest.fixture
def http_client(require_server) -> httpx.Client:
    with httpx.Client(timeout=60.0) as client:
        yield client


@pytest.fixture
def registered_owner(http_client: httpx.Client, api_base: str, unique_suffix: int) -> TenantContext:
    from tests.helpers.tenants import register_owner

    return register_owner(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"OwnerOnly {unique_suffix}",
        owner_name="Owner",
        phone_prefix="200",
    )


@pytest.fixture
def rbac_tenant(http_client: httpx.Client, api_base: str, unique_suffix: int) -> TenantContext:
    return build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"RBAC {unique_suffix}",
        owner_name="RBAC Owner",
        phone_prefix="310",
    )


@pytest.fixture
def pos_tenant(http_client: httpx.Client, api_base: str, unique_suffix: int) -> TenantContext:
    return build_pos_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="440",
    )
