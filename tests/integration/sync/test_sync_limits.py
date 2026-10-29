"""Per-device rate limits and configurable batch limits (own server with tiny limits)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.server import managed_server
from tests.helpers.sync import op, pull, push, sale_payload
from tests.helpers.tenants import build_pos_tenant

pytestmark = pytest.mark.integration


def test_per_device_rate_limits_and_configured_max_ops(unique_suffix):
    env = {
        "RATE_LIMIT_SYNC_PUSH_DEVICE": "3/minute",
        "RATE_LIMIT_SYNC_PULL_DEVICE": "2/minute",
        "SYNC_MAX_OPS": "5",
    }
    with managed_server(env) as api, httpx.Client(timeout=60.0) as client:
        t = build_pos_tenant(client, api, unique_suffix, phone_prefix="470")

        # The ops limit is checked before the rate limiter is charged.
        too_many = push(client, t, [op("sale.create", {}) for _ in range(6)], device_id="till-1")
        assert too_many.status_code == 413
        assert too_many.json()["detail"]["max_ops"] == 5

        for _ in range(3):
            assert push(client, t, [], device_id="till-1").status_code == 200
        limited = push(client, t, [], device_id="till-1")
        assert limited.status_code == 429 and "Retry-After" in limited.headers
        # Limits are per device: another till and the pull budget are untouched.
        assert push(client, t, [op("sale.create", sale_payload(t))], device_id="till-2").status_code == 200

        for _ in range(2):
            assert pull(client, t, device_id="till-1").status_code == 200
        assert pull(client, t, device_id="till-1").status_code == 429
        assert pull(client, t, device_id="till-2").status_code == 200
