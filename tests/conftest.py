"""Shared pytest fixtures for backend integration tests."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from tests.helpers.testdb import TestDatabaseConfigError, require_test_database_url
from tests.helpers.tenants import TenantContext, build_pos_tenant, build_rbac_tenant


def _use_test_database() -> None:
    """Point everything (this process and spawned servers) at TEST_DATABASE_URL.

    Runs when conftest is imported, i.e. before any ``app`` module builds its settings.
    """
    try:
        url = require_test_database_url()
    except TestDatabaseConfigError as exc:
        pytest.exit(str(exc), returncode=4)
    os.environ["DATABASE_URL"] = url


_use_test_database()


@pytest.fixture(scope="session")
def api_base() -> str:
    """API under test.

    ``API_BASE`` points at a server you started yourself (it must run with relaxed
    ``RATE_LIMIT_*`` values, because the suite registers many tenants from one IP).
    Without it, an isolated server with relaxed limits is started for the session
    against ``TEST_DATABASE_URL``. A server you start yourself must also run with
    ``DATABASE_URL`` set to the test database; the suite cannot verify that.
    """
    explicit = os.environ.get("API_BASE")
    if explicit:
        yield explicit
        return
    from tests.helpers.server import managed_server

    with managed_server() as base:
        yield base


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
