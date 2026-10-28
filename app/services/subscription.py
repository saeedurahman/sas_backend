"""Subscription state: trial, paid period, read-only grace, suspension.

Every business gets every feature; only time and status gate access. The state is
derived from columns already on ``businesses`` so ``get_current_user`` can evaluate it
without another query. Nothing here deletes data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

# status values reported to clients
TRIAL = "trial"
ACTIVE = "active"
GRACE = "grace"
EXPIRED = "expired"
SUSPENDED = "suspended"
CANCELLED = "cancelled"

CODE_EXPIRED = "subscription_expired"
CODE_SUSPENDED = "subscription_suspended"
CODE_CANCELLED = "subscription_cancelled"
CODE_GRACE_READONLY = "subscription_grace_readonly"

CONTACT_KEYS = {
    "contact_phone": "phone",
    "contact_whatsapp": "whatsapp",
    "contact_email": "email",
}
PAYMENT_INSTRUCTIONS_KEY = "payment_instructions"

_DAY = 86400.0


@dataclass(frozen=True)
class SubscriptionState:
    status: str
    trial_ends_at: datetime | None
    subscription_ends_at: datetime | None
    expires_at: datetime | None  # the date that matters for the current status
    days_left: int | None
    in_grace: bool

    @property
    def blocked_code(self) -> str | None:
        """Code for a full block (every call), or None."""
        return {
            EXPIRED: CODE_EXPIRED,
            SUSPENDED: CODE_SUSPENDED,
            CANCELLED: CODE_CANCELLED,
        }.get(self.status)

    @property
    def read_only(self) -> bool:
        return self.status == GRACE


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _days_left(end: datetime | None, now: datetime) -> int | None:
    if end is None:
        return None
    delta = (end - now).total_seconds() / _DAY
    return math.ceil(delta) if delta >= 0 else math.floor(delta)


def compute_state(
    business: Any,
    now: datetime | None = None,
    grace_days: int | None = None,
) -> SubscriptionState:
    """Effective subscription state of ``business`` (needs only its own columns)."""
    now = now or datetime.now(timezone.utc)
    grace = timedelta(days=settings.grace_days if grace_days is None else grace_days)
    trial_end = _aware(business.trial_ends_at)
    sub_end = _aware(business.subscription_ends_at)
    raw = business.subscription_status

    if raw == "suspended":
        return SubscriptionState(SUSPENDED, trial_end, sub_end, None, None, False)
    if raw == "cancelled":
        return SubscriptionState(CANCELLED, trial_end, sub_end, None, None, False)

    if raw == "trial":
        if trial_end is not None and now > trial_end:
            return SubscriptionState(
                EXPIRED, trial_end, sub_end, trial_end, _days_left(trial_end, now), False
            )
        return SubscriptionState(
            TRIAL, trial_end, sub_end, trial_end, _days_left(trial_end, now), False
        )

    # active / past_due: the paid period decides. No end date = not time-limited.
    if sub_end is None or now <= sub_end:
        return SubscriptionState(
            ACTIVE, trial_end, sub_end, sub_end, _days_left(sub_end, now), False
        )
    if now <= sub_end + grace:
        return SubscriptionState(
            GRACE, trial_end, sub_end, sub_end, _days_left(sub_end, now), True
        )
    return SubscriptionState(
        EXPIRED, trial_end, sub_end, sub_end, _days_left(sub_end, now), False
    )


def block_message(state: SubscriptionState, *, trial: bool = False) -> str:
    if state.status == SUSPENDED:
        return "Your account is suspended. Please contact support."
    if state.status == CANCELLED:
        return "Your subscription was cancelled. Please contact support."
    if state.status == GRACE:
        days = max(
            0,
            math.ceil(
                ((_aware(state.subscription_ends_at) or datetime.now(timezone.utc))
                 + timedelta(days=settings.grace_days)
                 - datetime.now(timezone.utc)).total_seconds()
                / _DAY
            ),
        )
        return (
            "Your subscription has expired and the app is read-only for "
            f"{days} more day{'s' if days != 1 else ''}. Please renew to keep working."
        )
    if trial:
        return "Your free trial has ended. Please subscribe to continue."
    return "Your subscription has expired. Please renew to continue."


class SubscriptionBlocked(Exception):
    """Raised by ``get_current_user``; rendered as HTTP 402 by an exception handler."""

    def __init__(
        self,
        code: str,
        message: str,
        expired_at: datetime | None,
        contact: dict[str, str],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.expired_at = expired_at
        self.contact = contact

    def body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            # Same text under "detail" so clients that only read FastAPI errors still show it.
            "detail": self.message,
            "expired_at": self.expired_at.isoformat() if self.expired_at else None,
            "contact": self.contact,
        }


async def load_platform_settings(db: AsyncSession) -> dict[str, str]:
    from app.models.platform import PlatformSetting

    rows = (await db.execute(select(PlatformSetting))).scalars().all()
    return {row.key: row.value for row in rows}


def contact_from_settings(values: dict[str, str]) -> dict[str, str]:
    return {name: values.get(key, "") or "" for key, name in CONTACT_KEYS.items()}


async def load_contact(db: AsyncSession) -> dict[str, str]:
    return contact_from_settings(await load_platform_settings(db))


async def build_blocked(
    db: AsyncSession, state: SubscriptionState, business: Any, *, grace_write: bool
) -> SubscriptionBlocked:
    contact = await load_contact(db)
    if grace_write:
        return SubscriptionBlocked(
            CODE_GRACE_READONLY,
            block_message(state),
            _aware(state.subscription_ends_at),
            contact,
        )
    code = state.blocked_code or CODE_EXPIRED
    trial = business.subscription_status == "trial"
    return SubscriptionBlocked(
        code, block_message(state, trial=trial), state.expires_at, contact
    )


def add_months(moment: datetime, months: int) -> datetime:
    """Calendar months (Jan 31 + 1 month = Feb 28/29), time of day preserved."""
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = moment.day
    while True:
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1


def summary(state: SubscriptionState) -> dict[str, Any]:
    """The compact block returned with logins: {status, days_left}."""
    return {"status": state.status, "days_left": state.days_left}


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
