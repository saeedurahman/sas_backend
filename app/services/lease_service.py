"""Issue subscription leases (see ``app.licensing.lease`` for the wire format)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.licensing import lease as lease_format
from app.services import subscription as sub


def compute_valid_until(business: Any, now: datetime | None = None) -> datetime:
    """``now + LEASE_DAYS``, capped by when the subscription stops being usable.

    * trial: ``trial_ends_at``
    * paid: ``subscription_ends_at + GRACE_DAYS`` (read-only grace still lets the till work)
    * no end date: not time-limited, so just ``now + LEASE_DAYS``
    * expired / suspended / cancelled: ``now`` (an already-lapsed lease)
    """
    now = now or datetime.now(timezone.utc)
    state = sub.compute_state(business, now)
    if state.status in (sub.EXPIRED, sub.SUSPENDED, sub.CANCELLED):
        return now
    horizon = now + timedelta(days=settings.lease_days)
    if state.status == sub.TRIAL:
        cap = state.expires_at
    elif state.expires_at is not None:  # active or grace
        cap = state.expires_at + timedelta(days=settings.grace_days)
    else:
        cap = None
    return min(horizon, cap) if cap is not None else horizon


def issue_lease(business: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """Signed lease for ``business``, or ``None`` when no signing key is configured."""
    private_key = settings.lease_private_key_effective
    if not private_key:
        return None
    now = now or datetime.now(timezone.utc)
    document = lease_format.build_lease_document(
        str(business.id), compute_valid_until(business, now), now
    )
    return lease_format.sign_lease(document, private_key)
