"""The subscription gate inside get_current_user: methods, whitelist flags, grace writes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.dependencies import _enforce_subscription, allow_in_grace, subscription_exempt
from app.services import subscription as sub

NOW = datetime.now(timezone.utc)


def req(method: str) -> Request:
    return Request({"type": "http", "method": method, "headers": [], "query_string": b""})


def business(status="active", ends=None, trial=None):
    return SimpleNamespace(
        subscription_status=status, subscription_ends_at=ends, trial_ends_at=trial, plan_id=None
    )


@pytest.fixture(autouse=True)
def _no_db_contact(monkeypatch):
    async def fake_contact(db):
        return {"phone": "1", "whatsapp": "2", "email": "e"}

    monkeypatch.setattr(sub, "load_contact", fake_contact)


def run(method, biz, *, exempt=False, grace_ok=False):
    async def go():
        r = req(method)
        if exempt:
            await subscription_exempt(r)
        if grace_ok:
            await allow_in_grace(r)
        return await _enforce_subscription(r, biz, db=None)

    return asyncio.run(go())


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_healthy_business_is_never_blocked(method):
    run(method, business("active", NOW + timedelta(days=5)))
    run(method, business("trial", trial=NOW + timedelta(days=5)))


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_grace_allows_reads(method):
    run(method, business("active", NOW - timedelta(hours=1)))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_grace_blocks_every_write_method(method):
    with pytest.raises(sub.SubscriptionBlocked) as exc:
        run(method, business("active", NOW - timedelta(hours=1)))
    assert exc.value.code == "subscription_grace_readonly"
    assert exc.value.expired_at is not None and exc.value.contact["phone"] == "1"


def test_grace_write_whitelist_only_applies_during_grace():
    run("POST", business("active", NOW - timedelta(hours=1)), grace_ok=True)
    with pytest.raises(sub.SubscriptionBlocked) as exc:
        run("POST", business("active", NOW - timedelta(days=10)), grace_ok=True)
    assert exc.value.code == "subscription_expired"


@pytest.mark.parametrize(
    "biz,code",
    [
        (business("active", NOW - timedelta(days=10)), "subscription_expired"),
        (business("trial", trial=NOW - timedelta(days=1)), "subscription_expired"),
        (business("suspended"), "subscription_suspended"),
        (business("cancelled"), "subscription_cancelled"),
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_blocked_states_refuse_everything(biz, code, method):
    with pytest.raises(sub.SubscriptionBlocked) as exc:
        run(method, biz)
    assert exc.value.code == code


def test_exempt_routes_pass_in_every_state():
    for biz in (
        business("active", NOW - timedelta(days=10)),
        business("active", NOW - timedelta(hours=1)),
        business("suspended"),
        business("cancelled"),
        business("trial", trial=NOW - timedelta(days=1)),
    ):
        for method in ("GET", "POST"):
            run(method, biz, exempt=True)
