"""Carry-over B: a retry with the just-rotated token gets the same pair back."""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.helpers.tenants import create_rbac_users, register_owner
from tests.security.conftest import login_raw

pytestmark = pytest.mark.integration


@pytest.fixture
def gtenant(client: httpx.Client, grace_api: str):
    ctx = register_owner(
        client,
        grace_api,
        uuid.uuid4().int % 10_000_000,
        business_name=f"Grace {uuid.uuid4().hex[:8]}",
        owner_name="Owner",
        phone_prefix="720",
    )
    return create_rbac_users(client, ctx)


def _refresh(client, api, token):
    return client.post(f"{api}/auth/refresh", json={"refresh_token": token})


def _login(client, api, tenant):
    r = login_raw(client, api, tenant.owner_phone, tenant.password)
    assert r.status_code == 200, r.text
    return r.json()


def test_retry_with_rotated_token_returns_identical_pair(client, grace_api, gtenant):
    first = _login(client, grace_api, gtenant)
    a = _refresh(client, grace_api, first["refresh_token"])
    b = _refresh(client, grace_api, first["refresh_token"])  # lost-response retry
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["refresh_token"] == b.json()["refresh_token"]
    assert a.json()["access_token"] == b.json()["access_token"]
    # The family was NOT revoked: the new refresh token still rotates.
    assert _refresh(client, grace_api, a.json()["refresh_token"]).status_code == 200


def test_replay_after_grace_window_revokes_family(client, grace_api, gtenant):
    first = _login(client, grace_api, gtenant)
    newest = _refresh(client, grace_api, first["refresh_token"]).json()["refresh_token"]
    time.sleep(5)
    assert _refresh(client, grace_api, first["refresh_token"]).status_code == 401
    assert _refresh(client, grace_api, newest).status_code == 401


def test_replay_after_successor_was_used_is_theft(client, grace_api, gtenant):
    first = _login(client, grace_api, gtenant)
    second = _refresh(client, grace_api, first["refresh_token"]).json()
    assert _refresh(client, grace_api, second["refresh_token"]).status_code == 200
    # ``first`` is inside the window, but its successor has already been spent.
    assert _refresh(client, grace_api, first["refresh_token"]).status_code == 401


def test_concurrent_double_submit_never_revokes_family(client, grace_api, gtenant):
    first = _login(client, grace_api, gtenant)
    token = first["refresh_token"]
    with httpx.Client(timeout=60.0) as c2:
        import concurrent.futures as cf

        with cf.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_refresh, c, grace_api, token) for c in (client, c2)
            ]
            results = [f.result() for f in futures]
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json()["refresh_token"] == results[1].json()["refresh_token"]
    assert (
        _refresh(client, grace_api, results[0].json()["refresh_token"]).status_code
        == 200
    )


def test_logout_inside_window_does_not_resurrect_session(client, grace_api, gtenant):
    first = _login(client, grace_api, gtenant)
    rotated = _refresh(client, grace_api, first["refresh_token"]).json()
    client.post(
        f"{grace_api}/auth/logout",
        headers={"Authorization": f"Bearer {rotated['access_token']}"},
        json={"refresh_token": rotated["refresh_token"]},
    )
    assert _refresh(client, grace_api, first["refresh_token"]).status_code == 401
