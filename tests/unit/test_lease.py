"""Subscription lease: signing, verification (tamper, expiry, rollback), valid_until rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.licensing import lease as lease_mod
from app.licensing.keys import generate_keypair
from app.services import lease_service

NOW = datetime(2026, 10, 29, 8, 0, 0, tzinfo=timezone.utc)
BUSINESS = "7d1c1a52-0d6a-4e0e-9b64-0f5d2f7b3a11"


@pytest.fixture(scope="module")
def keys():
    return generate_keypair()


def _signed(keys, *, valid_for=timedelta(days=7), issued=NOW, business=BUSINESS):
    document = lease_mod.build_lease_document(business, issued + valid_for, issued)
    return lease_mod.sign_lease(document, keys[0])


def test_roundtrip_returns_parsed_lease(keys):
    lease = _signed(keys)
    assert set(lease) == {"v", "business_id", "valid_until", "server_time", "signature"}
    parsed = lease_mod.verify_lease(lease, keys[1], now=NOW + timedelta(days=1), business_id=BUSINESS)
    assert parsed.business_id == BUSINESS
    assert parsed.valid_until == NOW + timedelta(days=7)
    assert parsed.server_time == NOW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid_until", "2027-10-29T08:00:00Z"),  # extend the lease
        ("server_time", "2026-10-01T08:00:00Z"),
        ("business_id", "11111111-1111-1111-1111-111111111111"),
        ("v", 2),
    ],
)
def test_tampered_field_fails_signature(keys, field, value):
    lease = _signed(keys)
    lease[field] = value
    with pytest.raises(lease_mod.LeaseBadSignature):
        lease_mod.verify_lease(lease, keys[1], now=NOW)


def test_tampered_signature_and_wrong_key(keys):
    lease = _signed(keys)
    flipped = {**lease, "signature": ("A" if lease["signature"][0] != "A" else "B") + lease["signature"][1:]}
    with pytest.raises(lease_mod.LeaseBadSignature):
        lease_mod.verify_lease(flipped, keys[1], now=NOW)
    other_public = generate_keypair()[1]
    with pytest.raises(lease_mod.LeaseBadSignature):
        lease_mod.verify_lease(lease, other_public, now=NOW)


def test_expiry_boundary(keys):
    lease = _signed(keys)
    end = NOW + timedelta(days=7)
    lease_mod.verify_lease(lease, keys[1], now=end - timedelta(seconds=1))
    with pytest.raises(lease_mod.LeaseExpired):
        lease_mod.verify_lease(lease, keys[1], now=end)
    with pytest.raises(lease_mod.LeaseExpired):
        lease_mod.verify_lease(lease, keys[1], now=end + timedelta(days=30))


def test_clock_rolled_back_is_refused_but_small_skew_tolerated(keys):
    lease = _signed(keys)
    lease_mod.verify_lease(lease, keys[1], now=NOW - timedelta(minutes=4))
    with pytest.raises(lease_mod.LeaseClockRolledBack):
        lease_mod.verify_lease(lease, keys[1], now=NOW - timedelta(hours=2))


def test_wrong_business_and_malformed(keys):
    lease = _signed(keys)
    with pytest.raises(lease_mod.LeaseWrongBusiness):
        lease_mod.verify_lease(lease, keys[1], now=NOW, business_id="other")
    with pytest.raises(lease_mod.LeaseMalformed):
        lease_mod.verify_lease({k: v for k, v in lease.items() if k != "signature"}, keys[1], now=NOW)
    with pytest.raises(lease_mod.LeaseMalformed):
        lease_mod.verify_lease({**lease, "extra": 1}, keys[1], now=NOW)
    with pytest.raises(lease_mod.LeaseMalformed):
        lease_mod.verify_lease("not a lease", keys[1], now=NOW)  # type: ignore[arg-type]


def test_signature_is_over_canonical_json_not_field_order(keys):
    lease = _signed(keys)
    shuffled = dict(reversed(list(lease.items())))
    lease_mod.verify_lease(shuffled, keys[1], now=NOW)


# ── valid_until ──────────────────────────────────────────────────────────────


def _business(status, *, trial_end=None, sub_end=None):
    return SimpleNamespace(
        id=BUSINESS, subscription_status=status, trial_ends_at=trial_end, subscription_ends_at=sub_end
    )


def test_valid_until_is_now_plus_lease_days_when_far_from_the_end(monkeypatch):
    monkeypatch.setattr(lease_service.settings, "lease_days", 7)
    business = _business("active", sub_end=NOW + timedelta(days=200))
    assert lease_service.compute_valid_until(business, NOW) == NOW + timedelta(days=7)


def test_valid_until_capped_by_subscription_end_plus_grace(monkeypatch):
    monkeypatch.setattr(lease_service.settings, "lease_days", 7)
    monkeypatch.setattr(lease_service.settings, "grace_days", 3)
    business = _business("active", sub_end=NOW + timedelta(days=2))
    assert lease_service.compute_valid_until(business, NOW) == NOW + timedelta(days=5)


def test_valid_until_during_grace_ends_with_grace(monkeypatch):
    monkeypatch.setattr(lease_service.settings, "lease_days", 7)
    monkeypatch.setattr(lease_service.settings, "grace_days", 3)
    business = _business("active", sub_end=NOW - timedelta(days=1))
    assert lease_service.compute_valid_until(business, NOW) == NOW + timedelta(days=2)


def test_trial_lease_ends_with_trial_without_grace(monkeypatch):
    monkeypatch.setattr(lease_service.settings, "lease_days", 7)
    business = _business("trial", trial_end=NOW + timedelta(days=3))
    assert lease_service.compute_valid_until(business, NOW) == NOW + timedelta(days=3)


@pytest.mark.parametrize(
    "business",
    [
        _business("active", sub_end=NOW - timedelta(days=30)),
        _business("trial", trial_end=NOW - timedelta(days=1)),
        _business("suspended"),
        _business("cancelled"),
    ],
)
def test_lapsed_subscription_gets_an_already_expired_lease(business):
    assert lease_service.compute_valid_until(business, NOW) == NOW


def test_no_end_date_is_just_lease_days(monkeypatch):
    monkeypatch.setattr(lease_service.settings, "lease_days", 4)
    assert lease_service.compute_valid_until(_business("active"), NOW) == NOW + timedelta(days=4)


def test_issue_lease_signs_with_configured_key_and_verifies(monkeypatch, keys):
    monkeypatch.setattr(lease_service.settings, "lease_signing_private_key", keys[0])
    monkeypatch.setattr(lease_service.settings, "lease_public_key", keys[1])
    monkeypatch.setattr(lease_service.settings, "lease_days", 7)
    business = _business("active", sub_end=NOW + timedelta(days=90))
    issued = lease_service.issue_lease(business, NOW)
    parsed = lease_mod.verify_lease(issued, keys[1], now=NOW, business_id=BUSINESS)
    assert parsed.valid_until == NOW + timedelta(days=7)


def test_issue_lease_falls_back_to_license_key_then_none(monkeypatch, keys):
    s = lease_service.settings
    monkeypatch.setattr(s, "lease_signing_private_key", "")
    monkeypatch.setattr(s, "lease_public_key", "")
    monkeypatch.setattr(s, "license_signing_private_key", keys[0])
    monkeypatch.setattr(s, "license_public_key", keys[1])
    business = _business("active", sub_end=NOW + timedelta(days=90))
    assert lease_service.issue_lease(business, NOW) is not None
    monkeypatch.setattr(s, "license_signing_private_key", "")
    assert lease_service.issue_lease(business, NOW) is None
