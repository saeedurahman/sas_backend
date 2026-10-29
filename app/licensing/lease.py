"""Subscription lease: a short, server-signed permission slip for offline use.

Pure functions (no database, no settings) built on the same Ed25519 helpers as the license
keys, so the offline app can embed this module with only the public key.

Wire format (``lease`` object, returned by ``GET /subscription/status`` and on sign-in)::

    {"v": 1, "business_id": "<uuid>", "valid_until": "2026-11-05T08:15:00Z",
     "server_time": "2026-10-29T08:15:00Z", "signature": "<base64url Ed25519>"}

The signature covers the canonical JSON (sorted keys, no whitespace) of every field except
``signature``. A device may keep working offline while ``now < valid_until``; a device clock
earlier than ``server_time`` means the clock was rolled back and the lease is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.licensing.keys import LicenseError, sign_document, utc_iso, verify_document

LEASE_VERSION = 1
# Tolerated device clock behind the server when the lease was issued.
CLOCK_SKEW = timedelta(minutes=5)


class LeaseError(LicenseError):
    """The lease must not be honoured."""


class LeaseMalformed(LeaseError):
    pass


class LeaseBadSignature(LeaseError):
    pass


class LeaseExpired(LeaseError):
    pass


class LeaseClockRolledBack(LeaseError):
    pass


class LeaseWrongBusiness(LeaseError):
    pass


@dataclass(frozen=True)
class Lease:
    v: int
    business_id: str
    valid_until: datetime
    server_time: datetime


def build_lease_document(
    business_id: str, valid_until: datetime, server_time: datetime
) -> dict[str, Any]:
    return {
        "v": LEASE_VERSION,
        "business_id": str(business_id),
        "valid_until": utc_iso(valid_until),
        "server_time": utc_iso(server_time),
    }


def sign_lease(document: dict[str, Any], private_key_b64: str) -> dict[str, Any]:
    return {**document, "signature": sign_document(document, private_key_b64)}


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LeaseMalformed("Bad timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise LeaseMalformed("Bad timestamp") from exc


def verify_lease(
    lease: dict[str, Any],
    public_key_b64: str,
    *,
    now: datetime | None = None,
    business_id: str | None = None,
) -> Lease:
    """Return the lease if genuine and currently valid, else raise a ``LeaseError``.

    Order: shape, signature, business, clock rollback, expiry.
    """
    if not isinstance(lease, dict):
        raise LeaseMalformed("Lease is not an object")
    document = {k: v for k, v in lease.items() if k != "signature"}
    signature = lease.get("signature")
    if not isinstance(signature, str) or not signature:
        raise LeaseMalformed("Lease has no signature")
    if set(document) != {"v", "business_id", "valid_until", "server_time"}:
        raise LeaseMalformed("Unexpected lease fields")
    if not verify_document(document, signature, public_key_b64):
        raise LeaseBadSignature("Lease signature does not match")
    if document["v"] != LEASE_VERSION:
        raise LeaseMalformed(f"Unsupported lease version {document['v']}")

    parsed = Lease(
        v=document["v"],
        business_id=str(document["business_id"]),
        valid_until=_parse_time(document["valid_until"]),
        server_time=_parse_time(document["server_time"]),
    )
    if business_id is not None and str(business_id) != parsed.business_id:
        raise LeaseWrongBusiness("Lease was issued for a different business")
    moment = now or datetime.now(timezone.utc)
    if moment < parsed.server_time - CLOCK_SKEW:
        raise LeaseClockRolledBack("Device clock is earlier than the lease was issued")
    if moment >= parsed.valid_until:
        raise LeaseExpired("Lease has expired")
    return parsed
