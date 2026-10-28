"""Platform admin TOTP, JWT separation from tenant tokens, rate-limit config, branch scope."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

from app.core import platform_security as ps
from app.core.config import ConfigurationError, Settings
from app.dependencies import allowed_branch_ids
from app.services.auth_service import TokenError, create_access_token, decode_token

# RFC 6238 appendix B secret ("12345678901234567890") in base32
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.fixture(autouse=True)
def _platform_secret(monkeypatch):
    monkeypatch.setattr(ps.settings, "platform_jwt_secret", "p" * 48)


def test_totp_matches_rfc6238_vectors():
    # 6-digit truncations of the RFC's 8-digit SHA1 vectors
    assert ps.totp_at(RFC_SECRET, 59) == "287082"
    assert ps.totp_at(RFC_SECRET, 1111111109) == "081804"
    assert ps.totp_at(RFC_SECRET, 2000000000) == "279037"


def test_totp_window_allows_one_step_of_clock_drift():
    t = 1_700_000_000
    code = ps.totp_at(RFC_SECRET, t)
    step = int(t // 30)
    assert ps.verify_totp(RFC_SECRET, code, at=t) == step
    assert ps.verify_totp(RFC_SECRET, code, at=t + 30) == step
    assert ps.verify_totp(RFC_SECRET, code, at=t - 30) == step
    assert ps.verify_totp(RFC_SECRET, code, at=t + 90) is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 456"])
def test_totp_rejects_malformed_codes(bad):
    assert ps.verify_totp(RFC_SECRET, bad) is None


def test_generated_secret_is_usable_and_unique():
    a, b = ps.generate_totp_secret(), ps.generate_totp_secret()
    assert a != b and len(a) >= 32
    assert ps.verify_totp(a, ps.totp_at(a)) is not None


def test_provisioning_uri_is_otpauth_with_issuer():
    uri = ps.provisioning_uri("owner@x.pk", RFC_SECRET)
    assert uri.startswith("otpauth://totp/")
    assert f"secret={RFC_SECRET}" in uri and "issuer=PakPOS%20Admin" in uri
    assert "owner%40x.pk" in uri or "owner@x.pk" in uri


def test_platform_token_round_trip_and_short_expiry():
    admin_id = uuid.uuid4()
    token, expires_in = ps.create_platform_token(admin_id, 3)
    payload = ps.decode_platform_token(token)
    assert payload["sub"] == str(admin_id) and payload["tv"] == 3
    assert payload["aud"] == ps.PLATFORM_AUDIENCE and payload["type"] == "platform_access"
    assert 0 < expires_in <= 60 * 60
    assert payload["exp"] - payload["iat"] == expires_in


def test_tenant_token_is_not_a_platform_token():
    tenant_token = create_access_token({"sub": str(uuid.uuid4()), "business_id": str(uuid.uuid4())})
    with pytest.raises(ps.PlatformTokenError):
        ps.decode_platform_token(tenant_token)


def test_platform_token_is_not_a_tenant_token():
    token, _ = ps.create_platform_token(uuid.uuid4(), 0)
    with pytest.raises(TokenError):
        decode_token(token)


def test_platform_token_signed_with_the_tenant_secret_is_rejected():
    from app.core.config import settings

    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()), "aud": ps.PLATFORM_AUDIENCE, "iss": ps.PLATFORM_ISSUER,
            "type": ps.PLATFORM_TOKEN_TYPE, "tv": 0, "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )
    with pytest.raises(ps.PlatformTokenError):
        ps.decode_platform_token(forged)


def test_wrong_audience_and_expired_tokens_are_rejected():
    secret = "p" * 48
    now = datetime.now(timezone.utc)
    base = {
        "sub": str(uuid.uuid4()), "iss": ps.PLATFORM_ISSUER, "type": ps.PLATFORM_TOKEN_TYPE, "tv": 0,
    }
    wrong_aud = jwt.encode(
        {**base, "aud": "someone-else", "exp": int((now + timedelta(hours=1)).timestamp())},
        secret, algorithm="HS256",
    )
    expired = jwt.encode(
        {**base, "aud": ps.PLATFORM_AUDIENCE, "exp": int((now - timedelta(seconds=5)).timestamp())},
        secret, algorithm="HS256",
    )
    for bad in (wrong_aud, expired):
        with pytest.raises(ps.PlatformTokenError):
            ps.decode_platform_token(bad)


def test_missing_platform_secret_disables_the_api(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(ps.settings, "platform_jwt_secret", "")
    with pytest.raises(HTTPException) as exc:
        ps.create_platform_token(uuid.uuid4(), 0)
    assert exc.value.status_code == 503


# ── PLATFORM_ADMIN_RATE_LIMITS ────────────────────────────────────────────────

BASE = dict(
    DATABASE_URL="postgresql+asyncpg://u:p@h/db",
    JWT_SECRET_KEY="a" * 40,
    JWT_REFRESH_SECRET_KEY="b" * 40,
)


def test_platform_rate_limits_defaults_and_overrides():
    defaults = Settings(_env_file=None, **BASE).platform_rate_limits
    assert defaults == {"login_ip": "10/minute", "login_account": "5/minute", "api": "300/minute"}
    custom = Settings(
        _env_file=None, PLATFORM_ADMIN_RATE_LIMITS="login_ip=3/minute, api=50/second", **BASE
    ).platform_rate_limits
    assert custom["login_ip"] == "3/minute" and custom["api"] == "50/second"
    assert custom["login_account"] == "5/minute"


@pytest.mark.parametrize("bad", ["nope=1/minute", "login_ip=fast", "login_ip=0/minute"])
def test_bad_platform_rate_limits_fail_loudly(bad):
    with pytest.raises((ConfigurationError, ValueError)):
        Settings(_env_file=None, PLATFORM_ADMIN_RATE_LIMITS=bad, **BASE).platform_rate_limits


def test_trial_and_grace_defaults():
    s = Settings(_env_file=None, **BASE)
    assert s.trial_days == 14 and s.grace_days == 3


def test_production_requires_distinct_strong_platform_secret():
    prod = dict(ENV="production", ALLOWED_ORIGINS="https://a.example", **BASE)
    with pytest.raises(ValueError, match="PLATFORM_JWT_SECRET"):
        Settings(_env_file=None, PLATFORM_JWT_SECRET="short", **prod)
    with pytest.raises(ValueError, match="must differ"):
        Settings(_env_file=None, PLATFORM_JWT_SECRET="a" * 40, **prod)  # == tenant secret
    with pytest.raises(ValueError, match="set together"):
        Settings(_env_file=None, LICENSE_PUBLIC_KEY="abc", **prod)  # half a key pair
    Settings(_env_file=None, PLATFORM_JWT_SECRET="Zq7" * 14, **prod)


# ── branch scope rules ────────────────────────────────────────────────────────

BIZ = uuid.uuid4()
B1, B2, B3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _user(roles, default_branch=None):
    user_roles = [
        SimpleNamespace(
            deleted_at=None,
            business_id=BIZ,
            branch_id=branch,
            role=SimpleNamespace(deleted_at=None, name=name),
        )
        for name, branch in roles
    ]
    return SimpleNamespace(business_id=BIZ, default_branch_id=default_branch, user_roles=user_roles)


def test_owner_sees_every_branch_even_with_an_assigned_branch():
    assert allowed_branch_ids(_user([("Owner", None)], default_branch=B1)) is None


def test_assigned_branch_limits_an_unscoped_role():
    assert allowed_branch_ids(_user([("Cashier", None)], default_branch=B1)) == {B1}


def test_business_wide_staff_without_a_branch_sees_everything():
    assert allowed_branch_ids(_user([("Manager", None)])) is None


def test_scoped_roles_are_honoured_and_union_with_the_assigned_branch():
    assert allowed_branch_ids(_user([("Cashier", B2)])) == {B2}
    assert allowed_branch_ids(_user([("Cashier", B2), ("Manager", B3)], default_branch=B1)) == {B1, B2, B3}


def test_deleted_role_assignments_do_not_grant_access():
    user = _user([("Cashier", B2)], default_branch=B1)
    user.user_roles[0].deleted_at = datetime.now(timezone.utc)
    assert allowed_branch_ids(user) == {B1}


def test_time_is_monotonic_enough_for_replay_guard_assumption():
    # the replay guard stores the integer time-step; steps only grow
    assert int(time.time() // 30) > 0
