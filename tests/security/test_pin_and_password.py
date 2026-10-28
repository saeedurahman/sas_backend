"""Items 6 and 7: PIN policy, change-password, admin password reset."""

from __future__ import annotations

import pytest

from app.services.auth_service import hash_pin
from tests.helpers.sqlexec import sql
from tests.security.conftest import login_raw, slug_of

pytestmark = pytest.mark.integration


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _uid(phone):
    return str(sql("SELECT id FROM users WHERE phone = :p", p=phone)[0][0])


def _login_body(client, api, phone, pw):
    r = login_raw(client, api, phone, pw)
    assert r.status_code == 200, r.text
    return r.json()


def _refresh_status(client, api, token):
    return client.post(f"{api}/auth/refresh", json={"refresh_token": token}).status_code


# -- PIN policy ---------------------------------------------------------------


def test_new_four_digit_pin_rejected_six_digit_accepted(client, sec_api, tenant):
    url = tenant.url(f"/users/{_uid(tenant.cashier_phone)}/pin")
    for bad in ("1234", "12345", "12ab56", ""):
        r = client.put(url, headers=tenant.owner_headers, json={"pin_code": bad})
        assert r.status_code == 422, bad
    ok = client.put(url, headers=tenant.owner_headers, json={"pin_code": "493827"})
    assert ok.status_code == 200


def test_legacy_four_digit_pin_still_logs_in(client, sec_api, tenant):
    uid = _uid(tenant.cashier_phone)
    sql("UPDATE users SET pin_hash = :h WHERE id = :i", h=hash_pin("1234"), i=uid)
    r = client.post(
        f"{sec_api}/auth/login/pin",
        json={
            "business_slug": slug_of(client, tenant),
            "user_id": uid,
            "pin_code": "1234",
        },
    )
    assert r.status_code == 200


def test_self_pin_change_requires_current_secret(client, sec_api, tenant):
    body = _login_body(client, sec_api, tenant.cashier_phone, tenant.password)
    headers = _bearer(body["access_token"])
    url = f"{sec_api}/users/{body['user']['id']}/pin"

    assert client.put(url, headers=headers, json={"pin_code": "593827"}).status_code == 400
    wrong = client.put(
        url, headers=headers, json={"pin_code": "593827", "current_password": "Nope1234"}
    )
    assert wrong.status_code == 403
    ok = client.put(
        url,
        headers=headers,
        json={"pin_code": "593827", "current_password": tenant.password},
    )
    assert ok.status_code == 200

    # A PIN exists now, so it can authorise the next change.
    bad_pin = client.put(
        url, headers=headers, json={"pin_code": "693827", "current_pin": "000000"}
    )
    assert bad_pin.status_code == 403
    good_pin = client.put(
        url, headers=headers, json={"pin_code": "693827", "current_pin": "593827"}
    )
    assert good_pin.status_code == 200


def test_pin_change_revokes_refresh_tokens(client, sec_api, tenant):
    body = _login_body(client, sec_api, tenant.cashier_phone, tenant.password)
    r = client.put(
        f"{sec_api}/users/{body['user']['id']}/pin",
        headers=_bearer(body["access_token"]),
        json={"pin_code": "593827", "current_password": tenant.password},
    )
    assert r.status_code == 200
    assert _refresh_status(client, sec_api, body["refresh_token"]) == 401


def test_owner_can_set_staff_pin_but_cashier_cannot(client, sec_api, tenant):
    url_mgr = tenant.url(f"/users/{_uid(tenant.manager_phone)}/pin")
    owner_set = client.put(
        url_mgr, headers=tenant.owner_headers, json={"pin_code": "384756"}
    )
    assert owner_set.status_code == 200
    cashier_set = client.put(
        url_mgr,
        headers=tenant.cashier_headers,
        json={"pin_code": "384756", "current_password": tenant.password},
    )
    assert cashier_set.status_code == 403


# -- change-password ----------------------------------------------------------


def test_change_password_flow_and_session_revocation(client, sec_api, tenant):
    body = _login_body(client, sec_api, tenant.cashier_phone, tenant.password)
    headers = _bearer(body["access_token"])
    url = f"{sec_api}/auth/change-password"

    wrong = client.post(
        url,
        headers=headers,
        json={"current_password": "Wrong1234", "new_password": "NewPass99"},
    )
    assert wrong.status_code == 403
    weak = client.post(
        url,
        headers=headers,
        json={"current_password": tenant.password, "new_password": "weak"},
    )
    assert weak.status_code == 422
    same = client.post(
        url,
        headers=headers,
        json={"current_password": tenant.password, "new_password": tenant.password},
    )
    assert same.status_code == 400
    ok = client.post(
        url,
        headers=headers,
        json={"current_password": tenant.password, "new_password": "NewPass99"},
    )
    assert ok.status_code == 200

    assert login_raw(client, sec_api, tenant.cashier_phone, tenant.password).status_code == 401
    assert login_raw(client, sec_api, tenant.cashier_phone, "NewPass99").status_code == 200
    assert _refresh_status(client, sec_api, body["refresh_token"]) == 401


def test_change_password_requires_authentication(client, sec_api):
    r = client.post(
        f"{sec_api}/auth/change-password",
        json={"current_password": "a", "new_password": "NewPass99"},
    )
    assert r.status_code == 401


# -- admin reset --------------------------------------------------------------


def test_manager_resets_cashier_password_and_unlocks(client, sec_api, tenant):
    cashier_id = _uid(tenant.cashier_phone)
    for _ in range(3):
        login_raw(client, sec_api, tenant.cashier_phone, "Wrong1234")
    locked = login_raw(client, sec_api, tenant.cashier_phone, tenant.password)
    assert locked.status_code == 403

    r = client.put(
        tenant.url(f"/users/{cashier_id}/password"),
        headers=tenant.manager_headers,
        json={"new_password": "Reset1234"},
    )
    assert r.status_code == 200
    assert login_raw(client, sec_api, tenant.cashier_phone, "Reset1234").status_code == 200


def test_reset_password_authorisation_rules(client, sec_api, tenant):
    owner_id = _uid(tenant.owner_phone)
    manager_id = _uid(tenant.manager_phone)
    cashier_id = _uid(tenant.cashier_phone)
    body = {"new_password": "Reset1234"}

    no_permission = client.put(
        tenant.url(f"/users/{manager_id}/password"),
        headers=tenant.cashier_headers,
        json=body,
    )
    assert no_permission.status_code == 403
    manager_vs_owner = client.put(
        tenant.url(f"/users/{owner_id}/password"),
        headers=tenant.manager_headers,
        json=body,
    )
    assert manager_vs_owner.status_code == 403
    self_reset = client.put(
        tenant.url(f"/users/{manager_id}/password"),
        headers=tenant.manager_headers,
        json=body,
    )
    assert self_reset.status_code == 400
    weak = client.put(
        tenant.url(f"/users/{cashier_id}/password"),
        headers=tenant.owner_headers,
        json={"new_password": "short"},
    )
    assert weak.status_code == 422
    ok = client.put(
        tenant.url(f"/users/{cashier_id}/password"),
        headers=tenant.owner_headers,
        json=body,
    )
    assert ok.status_code == 200


def test_reset_revokes_target_sessions(client, sec_api, tenant):
    cashier = _login_body(client, sec_api, tenant.cashier_phone, tenant.password)
    r = client.put(
        tenant.url(f"/users/{cashier['user']['id']}/password"),
        headers=tenant.owner_headers,
        json={"new_password": "Reset1234"},
    )
    assert r.status_code == 200
    assert _refresh_status(client, sec_api, cashier["refresh_token"]) == 401
