"""Carry-over C: credential changes invalidate already-issued access tokens."""

from __future__ import annotations

import pytest

from tests.security.conftest import login_raw

pytestmark = pytest.mark.integration


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, api, phone, pw):
    r = login_raw(client, api, phone, pw)
    assert r.status_code == 200, r.text
    return r.json()


def _me(client, api, token):
    return client.get(f"{api}/auth/me", headers=_bearer(token)).status_code


def test_change_password_invalidates_access_token(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.cashier_phone, tenant.password)
    assert _me(client, sec_api, body["access_token"]) == 200
    r = client.post(
        f"{sec_api}/auth/change-password",
        headers=_bearer(body["access_token"]),
        json={"current_password": tenant.password, "new_password": "NewPass123"},
    )
    assert r.status_code == 200, r.text
    assert _me(client, sec_api, body["access_token"]) == 401
    fresh = _login(client, sec_api, tenant.cashier_phone, "NewPass123")
    assert _me(client, sec_api, fresh["access_token"]) == 200


def test_admin_password_reset_invalidates_target_tokens(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.cashier_phone, tenant.password)
    uid = body["user"]["id"]
    r = client.put(
        f"{sec_api}/users/{uid}/password",
        headers=tenant.owner_headers,
        json={"new_password": "ResetPass123"},
    )
    assert r.status_code == 200, r.text
    assert _me(client, sec_api, body["access_token"]) == 401
    # The owner's own token is untouched.
    assert _me(client, sec_api, tenant.owner_headers["Authorization"][7:]) == 200


def test_pin_set_by_owner_invalidates_target_tokens(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.cashier_phone, tenant.password)
    r = client.put(
        f"{sec_api}/users/{body['user']['id']}/pin",
        headers=tenant.owner_headers,
        json={"pin_code": "483726"},
    )
    assert r.status_code == 200, r.text
    assert _me(client, sec_api, body["access_token"]) == 401


def test_deactivating_user_invalidates_tokens(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.cashier_phone, tenant.password)
    r = client.put(
        f"{sec_api}/users/{body['user']['id']}",
        headers=tenant.owner_headers,
        json={"is_active": False},
    )
    assert r.status_code == 200, r.text
    assert _me(client, sec_api, body["access_token"]) in (401, 403)


def test_token_without_tv_claim_is_treated_as_version_zero(client, sec_api, tenant):
    import jwt

    from app.core.config import settings
    from tests.helpers.sqlexec import sql

    body = _login(client, sec_api, tenant.cashier_phone, tenant.password)
    claims = jwt.decode(
        body["access_token"], settings.jwt_secret_key, algorithms=["HS256"]
    )
    claims.pop("tv")
    legacy = jwt.encode(claims, settings.jwt_secret_key, algorithm="HS256")
    assert _me(client, sec_api, legacy) == 200  # version 0 == users.token_version 0
    sql(
        "UPDATE users SET token_version = 5 WHERE id = :i", i=body["user"]["id"]
    )
    assert _me(client, sec_api, legacy) == 401
