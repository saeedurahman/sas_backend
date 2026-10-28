"""Item 2: failed-login bookkeeping must survive the 401/403 response."""

from __future__ import annotations

import pytest

from tests.helpers.sqlexec import sql
from tests.security.conftest import login_raw

pytestmark = pytest.mark.integration  # needs the shared dev database


def _user_state(phone: str):
    return sql(
        "SELECT failed_login_attempts, is_locked, locked_until FROM users "
        "WHERE phone = :p AND deleted_at IS NULL",
        p=phone,
    )[0]


def _audit_events(phone: str, event: str) -> int:
    return sql(
        """
        SELECT COUNT(*) FROM audit_logs a JOIN users u ON u.id = a.record_id
        WHERE u.phone = :p AND a.new_values->>'event' = :e
        """,
        p=phone,
        e=event,
    )[0][0]


def test_failed_attempts_are_persisted_and_audited(client, sec_api, tenant):
    phone = tenant.cashier_phone
    r = login_raw(client, sec_api, phone, "WrongPass1")
    assert r.status_code == 401
    assert "2 attempts remaining" in r.json()["detail"]
    attempts, locked, _ = _user_state(phone)
    assert attempts == 1 and locked is False
    assert _audit_events(phone, "login_failed") == 1


def test_n_wrong_passwords_lock_and_lock_persists(client, sec_api, tenant):
    phone = tenant.cashier_phone
    assert login_raw(client, sec_api, phone, "WrongPass1").status_code == 401
    assert login_raw(client, sec_api, phone, "WrongPass1").status_code == 401
    last = login_raw(client, sec_api, phone, "WrongPass1")  # 3rd = MAX_LOGIN_ATTEMPTS
    assert last.status_code == 403
    assert "locked" in last.json()["detail"].lower()

    attempts, locked, locked_until = _user_state(phone)
    assert locked is True and attempts == 3 and locked_until is not None
    assert _audit_events(phone, "account_locked") == 1
    assert _audit_events(phone, "login_failed") == 3


def test_correct_password_rejected_while_locked(client, sec_api, tenant):
    phone = tenant.cashier_phone
    for _ in range(3):
        login_raw(client, sec_api, phone, "WrongPass1")
    ok = login_raw(client, sec_api, phone, tenant.password)
    assert ok.status_code == 403
    assert "locked" in ok.json()["detail"].lower()
    # The rejected attempt must not reset the lock.
    assert _user_state(phone)[1] is True


def test_lock_expires_and_next_failure_starts_fresh(client, sec_api, tenant):
    phone = tenant.cashier_phone
    for _ in range(3):
        login_raw(client, sec_api, phone, "WrongPass1")
    sql(
        "UPDATE users SET locked_until = NOW() - INTERVAL '1 minute' WHERE phone = :p",
        p=phone,
    )
    # A wrong guess after expiry counts as attempt 1, not an instant re-lock.
    r = login_raw(client, sec_api, phone, "WrongPass1")
    assert r.status_code == 401
    attempts, locked, _ = _user_state(phone)
    assert (attempts, locked) == (1, False)

    ok = login_raw(client, sec_api, phone, tenant.password)
    assert ok.status_code == 200
    assert _user_state(phone)[0] == 0


def test_expired_lock_allows_correct_password(client, sec_api, tenant):
    phone = tenant.cashier_phone
    for _ in range(3):
        login_raw(client, sec_api, phone, "WrongPass1")
    sql(
        "UPDATE users SET locked_until = NOW() - INTERVAL '1 second' WHERE phone = :p",
        p=phone,
    )
    ok = login_raw(client, sec_api, phone, tenant.password)
    assert ok.status_code == 200
    attempts, locked, until = _user_state(phone)
    assert (attempts, locked, until) == (0, False, None)


def test_pin_failures_lock_and_persist(client, sec_api, tenant):
    from tests.helpers.assertions import assert_status

    cashier_id = sql("SELECT id FROM users WHERE phone = :p", p=tenant.cashier_phone)[0][0]
    # Owner sets a (new-style, 6 digit) PIN for the cashier.
    r = client.put(
        tenant.url(f"/users/{cashier_id}/pin"),
        headers=tenant.owner_headers,
        json={"pin_code": "482916"},
    )
    assert_status(r, 200, label="owner sets pin")
    slug = client.get(tenant.url("/auth/me"), headers=tenant.owner_headers).json()[
        "business_slug"
    ]
    body = {"business_slug": slug, "user_id": str(cashier_id)}
    for _ in range(2):
        assert (
            client.post(
                f"{sec_api}/auth/login/pin", json={**body, "pin_code": "111111"}
            ).status_code
            == 401
        )
    third = client.post(f"{sec_api}/auth/login/pin", json={**body, "pin_code": "111111"})
    assert third.status_code == 403
    assert _user_state(tenant.cashier_phone)[1] is True
    good = client.post(f"{sec_api}/auth/login/pin", json={**body, "pin_code": "482916"})
    assert good.status_code == 403
