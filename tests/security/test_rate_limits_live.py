"""Items 3 and 8: rate limits and registration abuse controls against a live server."""

from __future__ import annotations

import itertools
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import httpx
import pytest

from tests.helpers.server import managed_server
from tests.helpers.tenants import DEFAULT_PASSWORD, create_rbac_users, register_owner

pytestmark = pytest.mark.integration

_octet = itertools.count(1)


def _ip_client() -> httpx.Client:
    """A client that presents a unique (trusted) X-Forwarded-For address."""
    n = next(_octet) + uuid.uuid4().int % 50_000
    ip = f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"
    return httpx.Client(timeout=60.0, headers={"X-Forwarded-For": ip})


@pytest.fixture(scope="module")
def limited_api():
    with managed_server(
        {
            "TRUST_FORWARDED_FOR": "true",
            "RATE_LIMIT_LOGIN_IP": "3/minute",
            "RATE_LIMIT_LOGIN_ACCOUNT": "2/minute",
            "RATE_LIMIT_PIN_IP": "100000/minute",
            "RATE_LIMIT_PIN_USER": "2/minute",
            "RATE_LIMIT_REFRESH_IP": "2/minute",
            "RATE_LIMIT_REGISTER_IP": "2/hour",
            "RATE_LIMIT_CHECK_ALERTS_USER": "1/minute",
            "RATE_LIMIT_PASSWORD_CHANGE_USER": "2/minute",
            "RATE_LIMIT_PIN_CHANGE_USER": "2/minute",
        }
    ) as api:
        yield api


def _assert_429(resp: httpx.Response):
    assert resp.status_code == 429, resp.text
    assert int(resp.headers["retry-after"]) >= 1
    assert "too many" in resp.json()["detail"].lower()


def _register(client, api):
    return client.post(
        f"{api}/auth/register",
        json={
            "business_name": f"RL {uuid.uuid4().hex[:8]}",
            "business_type_code": "retail",
            "owner_name": "Owner",
            "owner_phone": f"713{uuid.uuid4().int % 10_000_000:07d}",
            "owner_password": DEFAULT_PASSWORD,
        },
    )


def test_register_limited_per_ip(limited_api):
    with _ip_client() as c:
        assert _register(c, limited_api).status_code == 201
        assert _register(c, limited_api).status_code == 201
        _assert_429(_register(c, limited_api))
    # A different client IP is unaffected.
    with _ip_client() as other:
        assert _register(other, limited_api).status_code == 201


def test_login_limited_per_ip(limited_api):
    with _ip_client() as c:
        for _ in range(3):
            r = c.post(f"{limited_api}/auth/login", json={"phone": f"72{uuid.uuid4().int % 10**8:08d}", "password": "Whatever1"})
            assert r.status_code == 401
        _assert_429(
            c.post(f"{limited_api}/auth/login", json={"phone": "7200000000", "password": "Whatever1"})
        )


def test_login_limited_per_account_across_ips(limited_api):
    phone = f"73{uuid.uuid4().int % 10**8:08d}"
    body = {"phone": phone, "password": "Whatever1"}
    for _ in range(2):
        with _ip_client() as c:
            assert c.post(f"{limited_api}/auth/login", json=body).status_code == 401
    with _ip_client() as c:
        _assert_429(c.post(f"{limited_api}/auth/login", json=body))
        other = c.post(
            f"{limited_api}/auth/login",
            json={"phone": f"74{uuid.uuid4().int % 10**8:08d}", "password": "Whatever1"},
        )
        assert other.status_code == 401  # a different account is not throttled


def test_pin_limited_per_user_across_ips(limited_api):
    body = {"business_slug": "nope", "user_id": str(uuid.uuid4()), "pin_code": "123456"}
    for _ in range(2):
        with _ip_client() as c:
            assert c.post(f"{limited_api}/auth/login/pin", json=body).status_code == 404
    with _ip_client() as c:
        _assert_429(c.post(f"{limited_api}/auth/login/pin", json=body))


def test_refresh_limited_per_ip(limited_api):
    with _ip_client() as c:
        for _ in range(2):
            assert (
                c.post(f"{limited_api}/auth/refresh", json={"refresh_token": "junk"}).status_code
                == 401
            )
        _assert_429(c.post(f"{limited_api}/auth/refresh", json={"refresh_token": "junk"}))


def test_check_alerts_is_throttled(limited_api):
    with _ip_client() as c:
        ctx = register_owner(
            c,
            limited_api,
            uuid.uuid4().int % 10_000_000,
            business_name=f"Alerts {uuid.uuid4().hex[:6]}",
            owner_name="Owner",
            phone_prefix="714",
        )
        create_rbac_users(c, ctx)
        first = c.post(ctx.url("/notifications/check-alerts"), headers=ctx.manager_headers)
        assert first.status_code == 200
        _assert_429(c.post(ctx.url("/notifications/check-alerts"), headers=ctx.manager_headers))
        # Limit is per user, not shared with the owner.
        assert (
            c.post(ctx.url("/notifications/check-alerts"), headers=ctx.owner_headers).status_code
            == 200
        )


def test_password_change_is_throttled(limited_api):
    with _ip_client() as c:
        ctx = register_owner(
            c,
            limited_api,
            uuid.uuid4().int % 10_000_000,
            business_name=f"PwRl {uuid.uuid4().hex[:6]}",
            owner_name="Owner",
            phone_prefix="715",
        )
        url = f"{limited_api}/auth/change-password"
        body = {"current_password": "Wrong1234", "new_password": "NewPass99"}
        for _ in range(2):
            assert c.post(url, headers=ctx.owner_headers, json=body).status_code == 403
        _assert_429(c.post(url, headers=ctx.owner_headers, json=body))


# -- captcha hook -------------------------------------------------------------


class _CaptchaStub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        ok = form.get("response") == ["good-token"] and form.get("secret") == ["stub-secret"]
        payload = json.dumps({"success": ok}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


@pytest.fixture(scope="module")
def captcha_api():
    stub = HTTPServer(("127.0.0.1", 0), _CaptchaStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    env = {
        "REGISTRATION_CAPTCHA_ENABLED": "true",
        "CAPTCHA_SECRET": "stub-secret",
        "CAPTCHA_VERIFY_URL": f"http://127.0.0.1:{stub.server_address[1]}/verify",
    }
    try:
        with managed_server(env) as api:
            yield api
    finally:
        stub.shutdown()


def _register_payload(**extra):
    return {
        "business_name": f"Cap {uuid.uuid4().hex[:8]}",
        "business_type_code": "retail",
        "owner_name": "Owner",
        "owner_phone": f"716{uuid.uuid4().int % 10_000_000:07d}",
        "owner_password": DEFAULT_PASSWORD,
        **extra,
    }


def test_captcha_enforced_when_enabled(captcha_api):
    with httpx.Client(timeout=60) as c:
        missing = c.post(f"{captcha_api}/auth/register", json=_register_payload())
        assert missing.status_code == 400
        assert missing.json()["detail"] == "Captcha verification failed"
        bad = c.post(
            f"{captcha_api}/auth/register", json=_register_payload(captcha_token="bad-token")
        )
        assert bad.status_code == 400
        good = c.post(
            f"{captcha_api}/auth/register", json=_register_payload(captcha_token="good-token")
        )
        assert good.status_code == 201


def test_captcha_provider_outage_fails_closed():
    env = {
        "REGISTRATION_CAPTCHA_ENABLED": "true",
        "CAPTCHA_SECRET": "stub-secret",
        "CAPTCHA_VERIFY_URL": "http://127.0.0.1:9/verify",  # nothing listens
    }
    with managed_server(env) as api, httpx.Client(timeout=60) as c:
        r = c.post(f"{api}/auth/register", json=_register_payload(captcha_token="good-token"))
        assert r.status_code == 400


def test_captcha_disabled_by_default_and_token_ignored(sec_api):
    with httpx.Client(timeout=60) as c:
        r = c.post(f"{sec_api}/auth/register", json=_register_payload())
        assert r.status_code == 201
        r = c.post(f"{sec_api}/auth/register", json=_register_payload(captcha_token="anything"))
        assert r.status_code == 201


def test_register_rejects_malformed_phone(sec_api):
    with httpx.Client(timeout=60) as c:
        for bad in ("abc", "12345", "03001234567x", "+", ""):
            r = c.post(f"{sec_api}/auth/register", json=_register_payload(owner_phone=bad))
            assert r.status_code == 422, bad
