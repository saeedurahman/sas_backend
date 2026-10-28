"""Item 10 against a live app: headers, request IDs, body limit, DB-aware /health."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.server import managed_server

pytestmark = pytest.mark.integration


def test_health_checks_database(sec_api):
    base = sec_api.replace("/api/v1", "")
    r = httpx.get(f"{base}/health", timeout=10)
    assert r.status_code == 200
    assert r.json()["db"] == "up"


def test_health_returns_503_when_db_down():
    with managed_server({"DATABASE_URL": "postgresql+asyncpg://nobody:x@127.0.0.1:9/none"}) as api:
        base = api.replace("/api/v1", "")
        r = httpx.get(f"{base}/health", timeout=15)
        assert r.status_code == 503
        assert r.json()["db"] == "down"
        # no connection details leak
        assert "nobody" not in r.text and "127.0.0.1:9" not in r.text


def test_security_headers_and_request_id_on_api_responses(sec_api):
    r = httpx.post(f"{sec_api}/auth/login", json={"phone": "7000000000", "password": "x"})
    assert r.status_code in (401, 422)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-request-id"]
    echoed = httpx.get(
        f"{sec_api}/auth/me", headers={"X-Request-ID": "client-trace-0001"}
    )
    assert echoed.headers["x-request-id"] == "client-trace-0001"


def test_oversized_body_gets_413():
    with managed_server({"MAX_REQUEST_BODY_BYTES": "4096"}) as api:
        big = {"phone": "7000000000", "password": "x" * 10_000}
        r = httpx.post(f"{api}/auth/login", json=big)
        assert r.status_code == 413
        small = httpx.post(f"{api}/auth/login", json={"phone": "7000000000", "password": "x"})
        assert small.status_code == 401


def test_cors_only_allows_configured_origins():
    with managed_server({"ALLOWED_ORIGINS": "https://app.example.com"}) as api:
        base = api.replace("/api/v1", "")
        allowed = httpx.options(
            f"{base}/health",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed.headers.get("access-control-allow-origin") == "https://app.example.com"
        denied = httpx.options(
            f"{base}/health",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in denied.headers
