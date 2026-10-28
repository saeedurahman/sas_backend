"""Subscription state machine: trial, active, read-only grace, expiry, suspension."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import subscription as sub

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def biz(status="active", trial_end=None, sub_end=None, plan_id=None):
    return SimpleNamespace(
        subscription_status=status,
        trial_ends_at=trial_end,
        subscription_ends_at=sub_end,
        plan_id=plan_id,
        is_active=True,
    )


def state(business, grace=3):
    return sub.compute_state(business, NOW, grace_days=grace)


def test_trial_running():
    s = state(biz("trial", trial_end=NOW + 14 * DAY))
    assert (s.status, s.days_left, s.in_grace, s.blocked_code) == ("trial", 14, False, None)


def test_trial_has_no_grace_period():
    s = state(biz("trial", trial_end=NOW - timedelta(seconds=1)))
    assert s.status == "expired"
    assert s.blocked_code == sub.CODE_EXPIRED
    assert not s.in_grace


def test_trial_without_end_date_is_not_blocked():
    assert state(biz("trial")).status == "trial"


def test_active_within_period():
    s = state(biz("active", sub_end=NOW + 30 * DAY))
    assert (s.status, s.days_left, s.blocked_code) == ("active", 30, None)


def test_active_without_end_date_is_open_ended():
    assert state(biz("active")).status == "active"


def test_active_end_boundary_is_still_active():
    assert state(biz("active", sub_end=NOW)).status == "active"


def test_just_past_end_is_grace_and_read_only():
    s = state(biz("active", sub_end=NOW - timedelta(minutes=1)))
    assert s.status == "grace" and s.in_grace and s.read_only
    assert s.blocked_code is None  # reads still work; writes are refused by the dependency
    assert s.days_left == -1


def test_last_second_of_grace_is_still_grace_then_expired():
    end = NOW - 3 * DAY
    assert state(biz("active", sub_end=end)).status == "grace"
    assert state(biz("active", sub_end=end - timedelta(seconds=1))).status == "expired"


def test_grace_length_is_configurable():
    end = NOW - 2 * DAY
    assert state(biz("active", sub_end=end), grace=3).status == "grace"
    assert state(biz("active", sub_end=end), grace=1).status == "expired"
    assert state(biz("active", sub_end=end), grace=0).status == "expired"


def test_past_due_is_treated_like_active():
    assert state(biz("past_due", sub_end=NOW + DAY)).status == "active"
    assert state(biz("past_due", sub_end=NOW - DAY)).status == "grace"


@pytest.mark.parametrize(
    "stored,status,code",
    [
        ("suspended", "suspended", sub.CODE_SUSPENDED),
        ("cancelled", "cancelled", sub.CODE_CANCELLED),
    ],
)
def test_suspended_and_cancelled_block_regardless_of_dates(stored, status, code):
    s = state(biz(stored, sub_end=NOW + 100 * DAY, trial_end=NOW + 100 * DAY))
    assert (s.status, s.blocked_code) == (status, code)


def test_naive_datetimes_are_read_as_utc():
    naive_end = (NOW + 5 * DAY).replace(tzinfo=None)
    assert state(biz("active", sub_end=naive_end)).days_left == 5


def test_messages_are_plain_language():
    expired = state(biz("active", sub_end=NOW - 10 * DAY))
    assert "expired" in sub.block_message(expired).lower()
    trial = state(biz("trial", trial_end=NOW - DAY))
    assert "trial" in sub.block_message(trial, trial=True).lower()
    assert "suspended" in sub.block_message(state(biz("suspended"))).lower()
    assert "cancelled" in sub.block_message(state(biz("cancelled"))).lower()


def test_blocked_exception_body_shape():
    err = sub.SubscriptionBlocked(
        sub.CODE_EXPIRED, "msg", NOW, {"phone": "1", "whatsapp": "2", "email": "e@x.pk"}
    )
    body = err.body()
    assert body["code"] == "subscription_expired"
    assert body["message"] == "msg"
    assert body["expired_at"] == NOW.isoformat()
    assert body["contact"] == {"phone": "1", "whatsapp": "2", "email": "e@x.pk"}
    no_date = sub.SubscriptionBlocked(sub.CODE_SUSPENDED, "m", None, {})
    assert no_date.body()["expired_at"] is None


def test_contact_from_settings_maps_keys_and_defaults_to_empty():
    assert sub.contact_from_settings(
        {"contact_phone": "0300", "contact_whatsapp": "0301"}
    ) == {"phone": "0300", "whatsapp": "0301", "email": ""}


def test_add_months_clamps_to_month_end():
    jan31 = datetime(2026, 1, 31, 9, 30, tzinfo=timezone.utc)
    assert sub.add_months(jan31, 1) == datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc)
    assert sub.add_months(jan31, 13) == datetime(2027, 2, 28, 9, 30, tzinfo=timezone.utc)
    assert sub.add_months(datetime(2024, 1, 31, tzinfo=timezone.utc), 1).day == 29  # leap year
    assert sub.add_months(datetime(2026, 11, 15, tzinfo=timezone.utc), 3) == datetime(
        2027, 2, 15, tzinfo=timezone.utc
    )


def test_summary_is_status_and_days_left_only():
    assert sub.summary(state(biz("active", sub_end=NOW + 7 * DAY))) == {
        "status": "active",
        "days_left": 7,
    }


def test_no_branch_limits_or_plan_feature_gates_remain():
    from app.services import branch_service

    assert not hasattr(branch_service, "BRANCH_LIMITS")
    import inspect

    assert "subscription_plan" not in inspect.signature(branch_service.create_new_branch).parameters
