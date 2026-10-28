"""Business-local calendar helpers.

Document numbers, "today" dashboards and day buckets follow the business's own
timezone (``businesses.timezone``, default Asia/Karachi), never the server's.
"""

import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Business

DEFAULT_TIMEZONE = "Asia/Karachi"
_CACHE_TTL_SECONDS = 300.0
_tz_cache: dict[UUID, tuple[float, ZoneInfo]] = {}


def load_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


async def get_business_tz(db: AsyncSession, business_id: UUID) -> ZoneInfo:
    cached = _tz_cache.get(business_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    name = (
        await db.execute(select(Business.timezone).where(Business.id == business_id))
    ).scalar_one_or_none()
    zone = load_zone(name)
    _tz_cache[business_id] = (time.monotonic() + _CACHE_TTL_SECONDS, zone)
    return zone


def forget_business_tz(business_id: UUID) -> None:
    _tz_cache.pop(business_id, None)


def local_now(zone: ZoneInfo, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(zone)


def local_today(zone: ZoneInfo, now: datetime | None = None) -> date:
    return local_now(zone, now).date()


def local_date_key(zone: ZoneInfo, now: datetime | None = None) -> str:
    """``YYYYMMDD`` of the business-local day (document-number counter key)."""
    return local_now(zone, now).strftime("%Y%m%d")


def local_day_bounds_utc(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of ``day`` in ``zone`` expressed in UTC."""
    start = datetime.combine(day, dtime.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def business_today(db: AsyncSession, business_id: UUID) -> date:
    return local_today(await get_business_tz(db, business_id))
