"""Credentials for platform admins: TOTP (RFC 6238) and a JWT that tenant tokens cannot match.

Tenant tokens are signed with ``JWT_SECRET_KEY`` and carry no ``aud``; platform tokens use
``PLATFORM_JWT_SECRET``, ``aud=pakpos-platform`` and ``type=platform_access``. A token from
one side is rejected by the other twice over (signature, then audience/type).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from jwt.exceptions import InvalidTokenError

from app.core.config import settings

PLATFORM_AUDIENCE = "pakpos-platform"
PLATFORM_ISSUER = "pakpos-platform"
PLATFORM_TOKEN_TYPE = "platform_access"
TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_ISSUER = "PakPOS Admin"


class PlatformTokenError(Exception):
    pass


class PlatformEncryptionError(Exception):
    pass


# ── Encryption at rest (Fernet) ──────────────────────────────────────────────


def _get_fernet_key(key: str | None = None) -> bytes:
    if key:
        return key.encode() if isinstance(key, str) else key
    if settings.platform_encryption_key:
        return settings.platform_encryption_key.encode()
    # Dev / test fallback derived from JWT secret
    base_secret = settings.platform_jwt_secret or settings.jwt_secret_key
    digest = hashlib.sha256(base_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def is_fernet_token(val: str) -> bool:
    return val.startswith("gAAAAA") and len(val) >= 100


def encrypt_totp_secret(secret: str, key: str | None = None) -> str:
    if is_fernet_token(secret):
        return secret
    fernet = Fernet(_get_fernet_key(key))
    return fernet.encrypt(secret.encode()).decode()


def decrypt_totp_secret(val: str, key: str | None = None) -> str:
    if not is_fernet_token(val):
        return val
    fernet = Fernet(_get_fernet_key(key))
    try:
        return fernet.decrypt(val.encode()).decode()
    except InvalidToken as exc:
        raise PlatformEncryptionError("Failed to decrypt TOTP secret with platform key") from exc


# ── Recovery Codes ────────────────────────────────────────────────────────────


def generate_recovery_codes(n: int = 10) -> list[str]:
    """Generate n one-time backup recovery codes formatted like 'a1b2-c3d4'."""
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(n)]


def hash_recovery_code(admin_id: uuid.UUID, code: str) -> str:
    code_norm = code.strip().lower().replace("-", "")
    return hashlib.sha256(f"{admin_id}:{code_norm}".encode()).hexdigest()


def verify_and_consume_recovery_code(admin: Any, code: str) -> bool:
    hashes = list(getattr(admin, "recovery_codes_hashes", None) or [])
    if not hashes:
        return False
    h = hash_recovery_code(admin.id, code)
    if h in hashes:
        hashes.remove(h)
        admin.recovery_codes_hashes = hashes
        return True
    return False


# ── TOTP ──────────────────────────────────────────────────────────────────────


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (
        10**TOTP_DIGITS
    )
    return str(code).zfill(TOTP_DIGITS)


def totp_at(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at if at is not None else time.time()) // TOTP_PERIOD))


def verify_totp(
    secret: str, code: str, *, at: float | None = None, window: int = 1
) -> int | None:
    """Return the matched time-step (so a caller can refuse reuse), or ``None``."""
    code = (code or "").strip()
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    step_now = int((at if at is not None else time.time()) // TOTP_PERIOD)
    matched: int | None = None
    for step in range(step_now - window, step_now + window + 1):
        # evaluate every candidate so timing does not reveal which step matched
        if hmac.compare_digest(_hotp(secret, step), code):
            matched = step
    return matched


def provisioning_uri(email: str, secret: str) -> str:
    label = quote(f"{TOTP_ISSUER}:{email}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(TOTP_ISSUER)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )


# ── JWT ───────────────────────────────────────────────────────────────────────


def _secret() -> str:
    if not settings.platform_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform admin is not configured (PLATFORM_JWT_SECRET)",
        )
    return settings.platform_jwt_secret


def ensure_configured() -> None:
    _secret()


def create_platform_token(admin_id: uuid.UUID, token_version: int) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    now = datetime.now(timezone.utc)
    lifetime = timedelta(minutes=settings.platform_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(admin_id),
        "aud": PLATFORM_AUDIENCE,
        "iss": PLATFORM_ISSUER,
        "type": PLATFORM_TOKEN_TYPE,
        "tv": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, _secret(), algorithm="HS256")
    return token, int(lifetime.total_seconds())


def decode_platform_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            audience=PLATFORM_AUDIENCE,
            issuer=PLATFORM_ISSUER,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except InvalidTokenError as exc:
        raise PlatformTokenError("Invalid or expired token") from exc
    if payload.get("type") != PLATFORM_TOKEN_TYPE:
        raise PlatformTokenError("Not a platform token")
    return payload
