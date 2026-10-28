"""FastAPI dependencies for /platform/* (admin JWT only)."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.platform_security import PlatformTokenError, decode_platform_token
from app.core.rate_limit import client_ip, enforce_rate_limit
from app.database import get_db
from app.models.platform import PlatformAdmin

_bearer = HTTPBearer(auto_error=False, scheme_name="PlatformBearer")


def _unauthorized(detail: str = "Could not validate credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_platform_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> PlatformAdmin:
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        payload = decode_platform_token(credentials.credentials)
        from uuid import UUID

        admin_id = UUID(str(payload["sub"]))
    except (PlatformTokenError, ValueError, KeyError):
        raise _unauthorized() from None

    admin = await db.get(PlatformAdmin, admin_id)
    if admin is None or not admin.is_active:
        raise _unauthorized()
    if int(payload.get("tv", 0)) != admin.token_version:
        raise _unauthorized("Token is no longer valid")

    enforce_rate_limit("platform-api", str(admin.id), settings.platform_rate_limits["api"])
    return admin


def request_ip(request: Request) -> str:
    return client_ip(request)


get_platform_admin.__route_guard__ = "platform_admin"  # type: ignore[attr-defined]
