"""Platform admin sign-in: password + TOTP, lockout, replay, audience separation, rate limits."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.helpers.db import db_execute, db_rows, db_scalar
from tests.helpers.platform import (
    AdminCtx, audit_rows, code_for, create_admin_row, login_response, new_admin, root_url, sign_in,
)
from tests.helpers.server import managed_server
from tests.helpers.tenants import register_owner

pytestmark = pytest.mark.integration

GENERIC = "Invalid email, password or code"


@pytest.fixture
def fresh(http_client, root) -> AdminCtx:
    return create_admin_row(root)


def test_login_success_shape_and_short_lived_token(http_client, fresh):
    r = login_response(http_client, fresh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "admin"}
    assert body["token_type"] == "bearer"
    assert 0 < body["expires_in"] <= 3600
    assert body["admin"]["email"] == fresh.email and body["admin"]["id"] == fresh.id
    assert "password_hash" not in str(body) and "totp" not in str(body)
    me = http_client.get(fresh.url("/me"), headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == fresh.email
    assert db_scalar("SELECT last_login FROM platform_admins WHERE id = :i", {"i": fresh.id}) is not None


def test_email_is_case_insensitive(http_client, fresh):
    shout = AdminCtx(**{**fresh.__dict__, "email": fresh.email.upper()})
    assert login_response(http_client, shout).status_code == 200


def test_failures_are_indistinguishable(http_client, fresh):
    wrong_pw = login_response(http_client, fresh, password="Wrong-password-123")
    wrong_code = login_response(http_client, fresh, code="000000")
    unknown = login_response(http_client, AdminCtx(**{**fresh.__dict__, "email": "nobody@pakpos.test"}))
    for r in (wrong_pw, wrong_code, unknown):
        assert r.status_code == 401
        assert r.json()["detail"] == GENERIC


def test_totp_code_cannot_be_replayed(http_client, fresh):
    code = code_for(fresh)
    assert login_response(http_client, fresh, code=code).status_code == 200
    again = login_response(http_client, fresh, code=code, fresh_step=False)
    assert again.status_code == 401 and again.json()["detail"] == GENERIC
    # a later step is fine
    later = login_response(http_client, fresh, code=code_for(fresh, offset_steps=1), fresh_step=False)
    assert later.status_code == 200


def test_lockout_is_persisted_and_blocks_correct_credentials(http_client, fresh):
    for _ in range(3):  # PLATFORM_MAX_LOGIN_ATTEMPTS=3 in the test server
        assert login_response(http_client, fresh, password="Wrong-password-123").status_code == 401
    locked = login_response(http_client, fresh)
    assert locked.status_code == 403 and "locked" in locked.json()["detail"].lower()
    assert int(locked.headers["Retry-After"]) > 0
    row = db_rows("SELECT failed_login_attempts, locked_until FROM platform_admins WHERE id = :i", {"i": fresh.id})[0]
    assert row["failed_login_attempts"] >= 3 and row["locked_until"] is not None
    assert audit_rows("auth.locked", entity_id=fresh.id)
    # lock survives (it is in the database, not process memory); it ends when the time passes
    db_execute("UPDATE platform_admins SET locked_until = NOW() - INTERVAL '1 minute' WHERE id = :i", {"i": fresh.id})
    ok = login_response(http_client, fresh)
    assert ok.status_code == 200
    assert db_scalar("SELECT failed_login_attempts FROM platform_admins WHERE id = :i", {"i": fresh.id}) == 0


def test_wrong_totp_counts_towards_lockout_but_password_is_checked_first(http_client, fresh):
    for _ in range(3):
        assert login_response(http_client, fresh, code="000000").status_code == 401
    assert login_response(http_client, fresh).status_code == 403


def test_disabled_admin_cannot_sign_in(http_client, fresh):
    db_execute("UPDATE platform_admins SET is_active = FALSE WHERE id = :i", {"i": fresh.id})
    r = login_response(http_client, fresh)
    assert r.status_code == 401 and r.json()["detail"] == GENERIC


def test_login_validates_input(http_client, root):
    url = f"{root}/platform/auth/login"
    assert http_client.post(url, json={"email": "a@b.pk", "password": "x"}).status_code == 422
    assert http_client.post(url, json={"email": "a@b.pk", "password": "x", "totp_code": "12"}).status_code == 422


def test_login_and_failures_are_audited_without_secrets(http_client, fresh):
    login_response(http_client, fresh, password="Wrong-password-123")
    login_response(http_client, fresh)
    assert audit_rows("auth.login_failed", entity_id=fresh.id)
    assert audit_rows("auth.login", entity_id=fresh.id)
    dump = str(db_rows("SELECT * FROM platform_audit_log WHERE entity_id = :i", {"i": fresh.id}))
    assert "Wrong-password-123" not in dump and fresh.totp_secret not in dump


# ── token separation ──────────────────────────────────────────────────────────

PROTECTED = [
    ("GET", "/me"), ("GET", "/dashboard"), ("GET", "/businesses"), ("GET", f"/businesses/{uuid.uuid4()}"),
    ("PATCH", f"/businesses/{uuid.uuid4()}/subscription"), ("POST", f"/businesses/{uuid.uuid4()}/payments"),
    ("POST", f"/businesses/{uuid.uuid4()}/suspend"), ("POST", f"/businesses/{uuid.uuid4()}/activate"),
    ("GET", "/plans"), ("POST", "/plans"), ("GET", "/settings"), ("PUT", "/settings"),
    ("GET", "/admins"), ("POST", "/admins"), ("GET", "/audit"), ("GET", "/licenses"), ("POST", "/licenses"),
    ("GET", f"/licenses/{uuid.uuid4()}"), ("POST", f"/licenses/{uuid.uuid4()}/revoke"),
    ("POST", "/auth/refresh"), ("POST", "/auth/logout"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_platform_route_needs_an_admin_token(http_client, root, method, path):
    r = http_client.request(method, f"{root}/platform{path}", json={})
    assert r.status_code == 401, (method, path, r.status_code)


def test_tenant_token_is_rejected_by_platform_and_vice_versa(http_client, api_base, root, admin, unique_suffix):
    t = register_owner(http_client, api_base, unique_suffix, business_name=f"Sep {unique_suffix}",
                       owner_name="Owner", phone_prefix="530")
    assert http_client.get(f"{root}/platform/me", headers=t.owner_headers).status_code == 401
    assert http_client.get(f"{root}/platform/dashboard", headers=t.owner_headers).status_code == 401
    # an admin token is no use against the tenant API
    assert http_client.get(f"{api_base}/auth/me", headers=admin.headers).status_code == 401
    assert http_client.get(f"{api_base}/products", headers=admin.headers).status_code == 401


def test_garbage_and_expired_style_tokens_are_401(http_client, root):
    for bad in ("garbage", "a.b.c", "x"):
        r = http_client.get(f"{root}/platform/me", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401


def test_refresh_issues_a_new_working_token(http_client, fresh):
    sign_in(http_client, fresh)
    r = http_client.post(fresh.url("/auth/refresh"), headers=fresh.headers)
    assert r.status_code == 200
    new = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert http_client.get(fresh.url("/me"), headers=new).status_code == 200


def test_logout_is_audited(http_client, fresh):
    sign_in(http_client, fresh)
    assert http_client.post(fresh.url("/auth/logout"), headers=fresh.headers).status_code == 200
    assert audit_rows("auth.logout", entity_id=fresh.id)


# ── rate limits (own server with tiny limits) ─────────────────────────────────


def test_login_is_rate_limited_per_ip_and_per_account():
    env = {"PLATFORM_ADMIN_RATE_LIMITS": "login_ip=3/minute,login_account=100/minute,api=100/minute"}
    with managed_server(env) as base, httpx.Client(timeout=30) as client:
        root = root_url(base)
        payload = {"email": "nobody@pakpos.test", "password": "x", "totp_code": "123456"}
        codes = [client.post(f"{root}/platform/auth/login", json=payload).status_code for _ in range(5)]
        assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
        assert int(client.post(f"{root}/platform/auth/login", json=payload).headers["Retry-After"]) > 0

    env = {"PLATFORM_ADMIN_RATE_LIMITS": "login_ip=100/minute,login_account=2/minute,api=100/minute"}
    with managed_server(env) as base, httpx.Client(timeout=30) as client:
        root = root_url(base)
        payload = {"email": "target@pakpos.test", "password": "x", "totp_code": "123456"}
        codes = [client.post(f"{root}/platform/auth/login", json=payload).status_code for _ in range(4)]
        assert codes == [401, 401, 429, 429]
        other = {**payload, "email": "someone-else@pakpos.test"}
        assert client.post(f"{root}/platform/auth/login", json=other).status_code == 401


def test_api_calls_are_rate_limited_per_admin():
    env = {"PLATFORM_ADMIN_RATE_LIMITS": "login_ip=100/minute,login_account=100/minute,api=4/minute"}
    with managed_server(env) as base, httpx.Client(timeout=30) as client:
        admin = new_admin(client, root_url(base))
        codes = [client.get(admin.url("/me"), headers=admin.headers).status_code for _ in range(6)]
        assert codes[:4] == [200] * 4 and codes[4:] == [429, 429]


def test_signing_key_missing_gives_503_not_a_crash():
    env = {"LICENSE_SIGNING_PRIVATE_KEY": "", "LICENSE_PUBLIC_KEY": ""}
    with managed_server(env) as base, httpx.Client(timeout=30) as client:
        admin = new_admin(client, root_url(base))
        r = client.post(
            admin.url("/licenses"), headers=admin.headers,
            json={"customer_name": "X", "machine_id": "M-1", "updates_until": "2099-01-01"},
        )
        assert r.status_code == 503
        assert client.get(f"{root_url(base)}/licenses/revocations").status_code == 503


def test_platform_api_is_disabled_without_a_secret():
    with managed_server({"PLATFORM_JWT_SECRET": ""}) as base, httpx.Client(timeout=30) as client:
        admin = create_admin_row(root_url(base))
        r = login_response(client, admin)
        assert r.status_code == 503
