"""Refresh token persistence and revocation."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import RefreshToken


async def save_refresh_token(
    db: AsyncSession,
    user_id: UUID,
    business_id: UUID,
    jti: str,
    expires_at: datetime,
    ip_address: str | None,
    user_agent: str | None,
) -> RefreshToken:
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware UTC")

    token = RefreshToken(
        user_id=user_id,
        business_id=business_id,
        jti=jti,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(token)
    await db.flush()
    return token


async def revoke_refresh_token(db: AsyncSession, jti: str) -> bool:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount > 0


async def revoke_all_user_tokens(db: AsyncSession, user_id: UUID) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount


async def is_refresh_token_valid(db: AsyncSession, jti: str) -> bool:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(RefreshToken.id).where(
            RefreshToken.jti == jti,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
    )
    return result.scalar_one_or_none() is not None
