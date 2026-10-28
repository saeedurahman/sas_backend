"""Sliding-window rate limiting (in-process).

Limits are per process. When running several workers/instances, also enforce
limits at the reverse proxy or swap ``limiter`` for a shared-store backend that
implements ``hit``.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from app.core.config import settings

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(frozen=True)
class Rate:
    count: int
    window_seconds: int


def parse_rate(value: str) -> Rate:
    """Parse ``"10/minute"`` (units: second, minute, hour, day; plural allowed)."""
    try:
        count_raw, unit_raw = value.strip().lower().split("/", 1)
        unit = unit_raw.strip().rstrip("s")
        rate = Rate(count=int(count_raw), window_seconds=_UNITS[unit])
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid rate limit {value!r}; use e.g. '10/minute'") from exc
    if rate.count < 1:
        raise ValueError(f"Invalid rate limit {value!r}: count must be >= 1")
    return rate


class InMemoryRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._max_window: dict[str, int] = {}
        self._calls = 0

    def hit(self, key: str, rate: Rate) -> int | None:
        """Record a hit. Return ``None`` if allowed, else seconds until retry."""
        now = self._clock()
        self._calls += 1
        if self._calls % 1000 == 0:
            self._purge(now)

        bucket = self._hits.setdefault(key, deque())
        self._max_window[key] = max(self._max_window.get(key, 0), rate.window_seconds)
        cutoff = now - rate.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= rate.count:
            return max(1, math.ceil(bucket[0] + rate.window_seconds - now))
        bucket.append(now)
        return None

    def _purge(self, now: float) -> None:
        for key in list(self._hits):
            bucket = self._hits[key]
            window = self._max_window.get(key, 0)
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if not bucket:
                del self._hits[key]
                self._max_window.pop(key, None)

    def reset(self) -> None:
        self._hits.clear()
        self._max_window.clear()


limiter = InMemoryRateLimiter()


def enforce_rate_limit(scope: str, identifier: str, rate_value: str) -> None:
    """Raise 429 (with ``Retry-After``) when ``identifier`` exceeds ``rate_value``."""
    if not settings.rate_limit_enabled:
        return
    retry_after = limiter.hit(f"{scope}:{identifier}", parse_rate(rate_value))
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def client_ip(request: Request) -> str:
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(first))
            except ValueError:
                pass
    return request.client.host if request.client else "unknown"


def ip_rate_limit(scope: str, setting_name: str) -> Callable[[Request], None]:
    """FastAPI dependency limiting by client IP; the rate is read per request."""

    def _dependency(request: Request) -> None:
        enforce_rate_limit(scope, client_ip(request), getattr(settings, setting_name))

    return _dependency
