"""Refresh token persistence, rotation bookkeeping and revocation.

Queries use column selects (not the ORM entity) so a lookup never drags in the
user/business relationships.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import RefreshToken

# Expired rows are kept briefly so a replayed (stolen) token is still recognised.
_PURGE_GRACE = timedelta(days=7)


@dataclass(frozen=True)
class StoredRefreshToken:
    jti: str
    user_id: UUID
    business_id: UUID
    family_id: UUID | None
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


async def save_refresh_token(
    db: AsyncSession,
    user_id: UUID,
    business_id: UUID,
    jti: str,
    expires_at: datetime,
    ip_address: str | None,
    user_agent: str | None,
    family_id: UUID | None = None,
    created_at: datetime | None = None,
) -> RefreshToken:
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware UTC")

    token = RefreshToken(
        user_id=user_id,
        business_id=business_id,
        jti=jti,
        family_id=family_id,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db.add(token)
    await db.flush()
    return token


async def get_refresh_token(db: AsyncSession, jti: str) -> StoredRefreshToken | None:
    result = await db.execute(
        select(
            RefreshToken.jti,
            RefreshToken.user_id,
            RefreshToken.business_id,
            RefreshToken.family_id,
            RefreshToken.expires_at,
            RefreshToken.revoked_at,
            RefreshToken.created_at,
        ).where(RefreshToken.jti == jti)
    )
    row = result.first()
    if row is None:
        return None
    return StoredRefreshToken(*row)


async def get_family_successor(
    db: AsyncSession, token: StoredRefreshToken
) -> StoredRefreshToken | None:
    """The token minted by rotating ``token`` (next in its family), live or not."""
    if token.family_id is None:
        return None
    result = await db.execute(
        select(
            RefreshToken.jti,
            RefreshToken.user_id,
            RefreshToken.business_id,
            RefreshToken.family_id,
            RefreshToken.expires_at,
            RefreshToken.revoked_at,
            RefreshToken.created_at,
        )
        .where(
            RefreshToken.user_id == token.user_id,
            RefreshToken.family_id == token.family_id,
            RefreshToken.created_at > token.created_at,
        )
        .order_by(RefreshToken.created_at.asc())
        .limit(1)
    )
    row = result.first()
    return StoredRefreshToken(*row) if row is not None else None


async def revoke_refresh_token(db: AsyncSession, jti: str) -> bool:
    """Atomically revoke one token; ``False`` if it was already revoked/unknown."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount > 0


async def revoke_token_family(
    db: AsyncSession, user_id: UUID, family_id: UUID | None, jti: str | None = None
) -> int:
    """Revoke every live token in a family.

    Tokens issued before rotation have no family; for those the single token
    ``jti`` is revoked instead.
    """
    now = datetime.now(timezone.utc)
    stmt = update(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
    )
    if family_id is not None:
        stmt = stmt.where(RefreshToken.family_id == family_id)
    elif jti is not None:
        stmt = stmt.where(RefreshToken.jti == jti)
    else:
        return 0
    result = await db.execute(stmt.values(revoked_at=now))
    return result.rowcount


async def revoke_all_user_tokens(db: AsyncSession, user_id: UUID) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount


async def purge_stale_tokens(db: AsyncSession, user_id: UUID) -> None:
    """Delete this user's long-expired rows so rotation doesn't grow the table forever."""
    cutoff = datetime.now(timezone.utc) - _PURGE_GRACE
    await db.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.expires_at < cutoff
        )
    )


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
