"""
JWT and credential hashing — single source of truth for token encode/decode.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
from fastapi import HTTPException, status
from jwt.exceptions import InvalidTokenError
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TokenError(Exception):
    """Raised when a JWT cannot be decoded or validated."""


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_pin(pin: str) -> str:
    return pwd_context.hash(pin)


def verify_pin(plain_pin: str, hashed_pin: str) -> bool:
    return pwd_context.verify(plain_pin, hashed_pin)


def create_access_token(data: dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        hours=settings.access_token_expire_hours
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def create_pin_access_token(data: dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        hours=settings.pin_token_expire_hours
    )
    to_encode.update({"exp": expire, "type": "pin_access"})
    return jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def create_refresh_token(data: dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    jti = str(uuid.uuid4())
    to_encode.update({"exp": expire, "type": "refresh", "jti": jti})
    return jwt.encode(
        to_encode,
        settings.jwt_refresh_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate an access JWT."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except InvalidTokenError as exc:
        raise TokenError("Invalid or expired token") from exc
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    """Decode and validate a refresh JWT."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_refresh_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except InvalidTokenError as exc:
        raise TokenError("Invalid or expired token") from exc
    return payload


def get_user_id_from_token(payload: dict[str, Any]) -> UUID:
    """Extract user id from token payload (sub claim)."""
    sub = payload.get("sub")
    if not sub:
        raise TokenError("Token missing subject")
    try:
        return UUID(str(sub))
    except ValueError as exc:
        raise TokenError("Invalid token subject") from exc


def get_jti_from_token(token: str) -> str:
    """Decode refresh token and return jti claim."""
    try:
        payload = decode_refresh_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        ) from exc
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing jti claim",
        )
    return str(jti)
