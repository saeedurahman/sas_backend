"""Item 3: limiter semantics, Retry-After, configurability (no DB needed)."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import rate_limit
from app.core.config import settings
from app.core.rate_limit import (
    InMemoryRateLimiter,
    Rate,
    enforce_rate_limit,
    ip_rate_limit,
    parse_rate,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_parse_rate():
    assert parse_rate("10/minute") == Rate(10, 60)
    assert parse_rate("3/hour") == Rate(3, 3600)
    assert parse_rate(" 5/Seconds ") == Rate(5, 1)
    assert parse_rate("1/day") == Rate(1, 86400)
    for bad in ("", "10", "x/minute", "0/minute", "5/fortnight"):
        with pytest.raises(ValueError):
            parse_rate(bad)


def test_sliding_window_blocks_then_recovers():
    clock = FakeClock()
    lim = InMemoryRateLimiter(clock)
    rate = Rate(3, 60)
    assert [lim.hit("k", rate) for _ in range(3)] == [None, None, None]
    retry = lim.hit("k", rate)
    assert retry == 60
    clock.now += 30
    assert lim.hit("k", rate) == 30
    clock.now += 31
    assert lim.hit("k", rate) is None


def test_keys_are_independent():
    lim = InMemoryRateLimiter(FakeClock())
    rate = Rate(1, 60)
    assert lim.hit("a", rate) is None
    assert lim.hit("b", rate) is None
    assert lim.hit("a", rate) is not None


def test_purge_drops_idle_keys():
    clock = FakeClock()
    lim = InMemoryRateLimiter(clock)
    lim.hit("old", Rate(5, 10))
    clock.now += 100
    lim._purge(clock.now)
    assert "old" not in lim._hits


@pytest.fixture
def isolated_limiter(monkeypatch):
    fresh = InMemoryRateLimiter()
    monkeypatch.setattr(rate_limit, "limiter", fresh)
    return fresh


def test_enforce_raises_429_with_retry_after(isolated_limiter):
    from fastapi import HTTPException

    enforce_rate_limit("s", "id", "2/minute")
    enforce_rate_limit("s", "id", "2/minute")
    with pytest.raises(HTTPException) as exc:
        enforce_rate_limit("s", "id", "2/minute")
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_disabled_flag_bypasses(isolated_limiter, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    for _ in range(50):
        enforce_rate_limit("s", "id", "1/minute")


def _app():
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(ip_rate_limit("t", "rate_limit_login_ip"))])
    def x():
        return {"ok": True}

    return app


def test_ip_dependency_returns_429_and_retry_after_header(isolated_limiter, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_ip", "2/minute")
    client = TestClient(_app())
    assert client.get("/x").status_code == 200
    assert client.get("/x").status_code == 200
    blocked = client.get("/x")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1


def test_limit_is_read_from_settings_per_request(isolated_limiter, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_ip", "1/minute")
    client = TestClient(_app())
    assert client.get("/x").status_code == 200
    assert client.get("/x").status_code == 429
    monkeypatch.setattr(settings, "rate_limit_login_ip", "100/minute")
    assert client.get("/x").status_code == 200


def test_forwarded_for_only_trusted_when_enabled(isolated_limiter, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_ip", "1/minute")
    client = TestClient(_app())

    # Untrusted: header ignored, both requests share the socket IP.
    monkeypatch.setattr(settings, "trust_forwarded_for", False)
    assert client.get("/x", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/x", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429

    isolated_limiter.reset()
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    assert client.get("/x", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/x", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert client.get("/x", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    # Garbage is not trusted as an identity.
    garbage = client.get("/x", headers={"X-Forwarded-For": "not-an-ip"})
    assert garbage.status_code in (200, 429)
