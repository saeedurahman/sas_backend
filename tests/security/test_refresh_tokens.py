"""Item 5: refresh rotation, reuse detection, revocation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import settings
from tests.helpers.sqlexec import sql
from tests.security.conftest import login_raw

pytestmark = pytest.mark.integration


def _login(client, api, phone, password):
    r = login_raw(client, api, phone, password)
    assert r.status_code == 200, r.text
    return r.json()


def _refresh(client, api, token):
    return client.post(f"{api}/auth/refresh", json={"refresh_token": token})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _forge(sub, business_id, jti):
    return jwt.encode(
        {
            "sub": sub,
            "business_id": business_id,
            "type": "refresh",
            "jti": jti,
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        settings.jwt_refresh_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def test_rotation_issues_new_refresh_token_and_chain_works(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.owner_phone, tenant.password)
    r1 = _refresh(client, sec_api, body["refresh_token"])
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["refresh_token"] != body["refresh_token"]
    assert (
        client.get(
            f"{sec_api}/auth/me", headers=_bearer(data1["access_token"])
        ).status_code
        == 200
    )
    r2 = _refresh(client, sec_api, data1["refresh_token"])
    assert r2.status_code == 200
    assert r2.json()["refresh_token"] not in (
        body["refresh_token"],
        data1["refresh_token"],
    )


def test_reuse_of_rotated_token_revokes_whole_family(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.owner_phone, tenant.password)
    old = body["refresh_token"]
    newest = _refresh(client, sec_api, old).json()["refresh_token"]

    assert _refresh(client, sec_api, old).status_code == 401
    # The legitimate (newest) token of that family is now dead too.
    assert _refresh(client, sec_api, newest).status_code == 401

    events = sql(
        """
        SELECT COUNT(*) FROM audit_logs a JOIN users u ON u.id = a.record_id
        WHERE u.phone = :p AND a.new_values->>'event' = 'refresh_reuse_detected'
        """,
        p=tenant.owner_phone,
    )[0][0]
    assert events >= 1  # every presentation of a revoked token is logged


def test_reuse_does_not_touch_other_sessions(client, sec_api, tenant):
    session_a = _login(client, sec_api, tenant.owner_phone, tenant.password)
    session_b = _login(client, sec_api, tenant.owner_phone, tenant.password)
    _refresh(client, sec_api, session_a["refresh_token"])
    assert _refresh(client, sec_api, session_a["refresh_token"]).status_code == 401
    assert _refresh(client, sec_api, session_b["refresh_token"]).status_code == 200


def test_logout_revokes_the_session_family(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.owner_phone, tenant.password)
    rotated = _refresh(client, sec_api, body["refresh_token"]).json()
    out = client.post(
        f"{sec_api}/auth/logout",
        headers=_bearer(rotated["access_token"]),
        json={"refresh_token": rotated["refresh_token"]},
    )
    assert out.status_code == 200
    assert _refresh(client, sec_api, rotated["refresh_token"]).status_code == 401


def test_access_token_is_not_a_refresh_token(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.owner_phone, tenant.password)
    assert _refresh(client, sec_api, body["access_token"]).status_code == 401


def test_refresh_token_is_not_an_access_token(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.owner_phone, tenant.password)
    r = client.get(f"{sec_api}/auth/me", headers=_bearer(body["refresh_token"]))
    assert r.status_code == 401


def test_refresh_rejected_when_business_inactive(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.owner_phone, tenant.password)
    sql("UPDATE businesses SET is_active = FALSE WHERE id = :b", b=tenant.business_id)
    try:
        assert _refresh(client, sec_api, body["refresh_token"]).status_code == 401
    finally:
        sql("UPDATE businesses SET is_active = TRUE WHERE id = :b", b=tenant.business_id)
    # The family was revoked, so reactivating the business does not revive it.
    assert _refresh(client, sec_api, body["refresh_token"]).status_code == 401


def test_refresh_rejected_for_deactivated_user(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.cashier_phone, tenant.password)
    sql("UPDATE users SET status = 'inactive' WHERE phone = :p", p=tenant.cashier_phone)
    assert _refresh(client, sec_api, body["refresh_token"]).status_code == 401


def test_token_subject_must_match_stored_owner(client, sec_api, tenant):
    owner = _login(client, sec_api, tenant.owner_phone, tenant.password)
    other = _login(client, sec_api, tenant.cashier_phone, tenant.password)
    owner_jti = jwt.decode(
        owner["refresh_token"],
        settings.jwt_refresh_secret_key,
        algorithms=[settings.jwt_algorithm],
    )["jti"]
    forged = _forge(other["user"]["id"], tenant.business_id, owner_jti)
    assert _refresh(client, sec_api, forged).status_code == 401
    # The real owner token is untouched by the forgery attempt.
    assert _refresh(client, sec_api, owner["refresh_token"]).status_code == 200


def test_unknown_jti_rejected(client, sec_api, tenant):
    body = _login(client, sec_api, tenant.owner_phone, tenant.password)
    forged = _forge(body["user"]["id"], tenant.business_id, str(uuid.uuid4()))
    assert _refresh(client, sec_api, forged).status_code == 401
