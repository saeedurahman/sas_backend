"""License keys and signed revocation lists (pure functions, no database, no settings).

The same module is meant to be copied into the offline edition: it only needs the
``cryptography`` package and the vendor's public key.

Key format (one pasteable line)::

    PPOS-<base64url(canonical JSON payload)>.<base64url(Ed25519 signature)>

The signature covers the exact payload bytes that are embedded, so nothing has to be
re-serialised to verify. Payload (``v`` 1)::

    {"v":1,"license_id":"LIC-...","edition":"local","customer":"...",
     "machine_id":"...","issued_at":"2026-10-29T08:15:00Z","updates_until":"2027-10-29"}

Keys are 32 raw bytes encoded as unpadded base64url.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEY_PREFIX = "PPOS-"
PAYLOAD_VERSION = 1
EDITION_LOCAL = "local"


class LicenseError(Exception):
    """Base class: the key must not be accepted."""


class MalformedKey(LicenseError):
    pass


class BadSignature(LicenseError):
    pass


class UnsupportedVersion(LicenseError):
    pass


class WrongMachine(LicenseError):
    pass


class Revoked(LicenseError):
    pass


@dataclass(frozen=True)
class LicensePayload:
    v: int
    license_id: str
    edition: str
    customer: str
    machine_id: str
    issued_at: str  # ISO-8601 UTC, "Z" suffix
    updates_until: str  # YYYY-MM-DD

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "license_id": self.license_id,
            "edition": self.edition,
            "customer": self.customer,
            "machine_id": self.machine_id,
            "issued_at": self.issued_at,
            "updates_until": self.updates_until,
        }

    @property
    def updates_until_date(self) -> date:
        return date.fromisoformat(self.updates_until)


# ── encoding helpers ──────────────────────────────────────────────────────────


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64u_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise MalformedKey("Not valid base64url") from exc


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, ASCII only."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_machine_id(value: str) -> str:
    return "".join(value.split()).upper()


# ── keys ──────────────────────────────────────────────────────────────────────


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_key, public_key)`` as unpadded base64url of the raw 32 bytes."""
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return b64u_encode(private_raw), b64u_encode(public_raw)


def _private(private_key_b64: str) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(b64u_decode(private_key_b64.strip()))
    except (ValueError, MalformedKey) as exc:
        raise LicenseError("Invalid private key") from exc


def _public(public_key_b64: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(b64u_decode(public_key_b64.strip()))
    except (ValueError, MalformedKey) as exc:
        raise LicenseError("Invalid public key") from exc


def public_key_for(private_key_b64: str) -> str:
    raw = _private(private_key_b64).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return b64u_encode(raw)


# ── license keys ──────────────────────────────────────────────────────────────


def build_payload(
    *,
    license_id: str,
    customer: str,
    machine_id: str,
    issued_at: datetime,
    updates_until: date,
    edition: str = EDITION_LOCAL,
) -> LicensePayload:
    return LicensePayload(
        v=PAYLOAD_VERSION,
        license_id=license_id,
        edition=edition,
        customer=customer,
        machine_id=machine_id,
        issued_at=utc_iso(issued_at),
        updates_until=updates_until.isoformat(),
    )


def sign_license(payload: LicensePayload, private_key_b64: str) -> str:
    body = canonical_json(payload.as_dict())
    signature = _private(private_key_b64).sign(body)
    return f"{KEY_PREFIX}{b64u_encode(body)}.{b64u_encode(signature)}"


def verify_license(
    key_string: str,
    public_key_b64: str,
    *,
    machine_id: str | None = None,
    revoked_ids: Iterable[str] | None = None,
) -> LicensePayload:
    """Return the payload of a genuine key or raise a ``LicenseError`` subclass.

    ``machine_id`` (when given) must match the one the key was issued for.
    ``revoked_ids`` is the ``license_id`` set from a verified revocation list.
    """
    key = (key_string or "").strip()
    if not key.startswith(KEY_PREFIX) or key.count(".") != 1:
        raise MalformedKey("Not a PakPOS license key")
    body_b64, sig_b64 = key[len(KEY_PREFIX) :].split(".")
    body = b64u_decode(body_b64)
    signature = b64u_decode(sig_b64)

    try:
        _public(public_key_b64).verify(signature, body)
    except InvalidSignature as exc:
        raise BadSignature("Signature does not match") from exc

    try:
        data = json.loads(body)
        payload = LicensePayload(**data)
    except (ValueError, TypeError) as exc:
        raise MalformedKey("Payload is not a license") from exc
    if payload.v != PAYLOAD_VERSION:
        raise UnsupportedVersion(f"Unsupported license version {payload.v}")
    try:
        payload.updates_until_date  # noqa: B018 - validates the date
    except ValueError as exc:
        raise MalformedKey("Bad updates_until") from exc

    if machine_id is not None and normalize_machine_id(machine_id) != normalize_machine_id(
        payload.machine_id
    ):
        raise WrongMachine("License was issued for a different machine")
    if revoked_ids is not None and payload.license_id in set(revoked_ids):
        raise Revoked("License has been revoked")
    return payload


def updates_valid(payload: LicensePayload, build_date: date) -> bool:
    """True when a build released on ``build_date`` is covered by the license."""
    return build_date <= payload.updates_until_date


# ── signed documents (revocation list) ────────────────────────────────────────


def sign_document(document: dict[str, Any], private_key_b64: str) -> str:
    """Signature (base64url) over the canonical JSON of ``document`` (no signature field)."""
    return b64u_encode(_private(private_key_b64).sign(canonical_json(document)))


def verify_document(document: dict[str, Any], signature_b64: str, public_key_b64: str) -> bool:
    try:
        _public(public_key_b64).verify(b64u_decode(signature_b64), canonical_json(document))
    except (InvalidSignature, LicenseError):
        return False
    return True


def build_revocation_document(
    revoked: Iterable[tuple[str, datetime]], issued_at: datetime
) -> dict[str, Any]:
    return {
        "v": PAYLOAD_VERSION,
        "issued_at": utc_iso(issued_at),
        "revoked": [
            {"license_id": license_id, "revoked_at": utc_iso(at)}
            for license_id, at in sorted(revoked, key=lambda r: r[0])
        ],
    }


def verify_revocation_list(
    signed: dict[str, Any], public_key_b64: str
) -> set[str]:
    """Return the revoked license ids of a genuine list or raise ``BadSignature``."""
    signature = signed.get("signature", "")
    document = {k: v for k, v in signed.items() if k != "signature"}
    if not verify_document(document, signature, public_key_b64):
        raise BadSignature("Revocation list signature does not match")
    return {entry["license_id"] for entry in document.get("revoked", [])}
