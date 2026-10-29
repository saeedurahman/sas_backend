"""Authentication flows: password login, PIN login, token refresh/rotation."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.database import AsyncSessionLocal
from app.models.business import Business, BusinessConfig
from app.models.user import Role, RolePermission, User, UserRole
from app.dependencies import (
    permissions_version,
    user_permission_keys_for_response,
    user_roles_for_response,
)
from app.services.lease_service import issue_lease
from app.services.subscription import compute_state, summary
from app.services.audit_helper import log_auth_event
from app.services.auth_service import (
    TokenError,
    create_access_token,
    create_pin_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_user_id_from_token,
    verify_password,
    verify_pin,
)
from app.schemas.auth import (
    RegisterBusinessRequest,
    SubscriptionSummary,
    TokenResponse,
    UserInfo,
)
from app.services.registration_service import (
    business_slug_from_business,
    register_new_business,
)
from app.services.token_service import (
    StoredRefreshToken,
    get_family_successor,
    get_refresh_token,
    purge_stale_tokens,
    revoke_all_user_tokens,
    revoke_refresh_token,
    revoke_token_family,
    save_refresh_token,
)

logger = logging.getLogger("app.auth")

_REVOKED_DETAIL = "Token revoked or expired"


def _build_token_payload(user: User) -> dict[str, str | int | None]:
    return {
        "sub": str(user.id),
        "business_id": str(user.business_id),
        "branch_id": str(user.default_branch_id) if user.default_branch_id else None,
        "tv": user.token_version,
    }


async def _issue_refresh_token(
    db: AsyncSession,
    user: User,
    ip_address: str | None,
    user_agent: str | None,
    family_id: UUID | None = None,
    issued_at: datetime | None = None,
) -> str:
    """Create + persist a refresh token; a new login starts a new family."""
    family_id = family_id or uuid.uuid4()
    issued_at = issued_at or datetime.now(timezone.utc)
    refresh_token = create_refresh_token(
        {**_build_token_payload(user), "fid": str(family_id)}, issued_at=issued_at
    )
    await _save_refresh_from_token(
        db, user, refresh_token, ip_address, user_agent, created_at=issued_at
    )
    return refresh_token


async def _save_refresh_from_token(
    db: AsyncSession,
    user: User,
    refresh_token: str,
    ip_address: str | None,
    user_agent: str | None,
    created_at: datetime | None = None,
) -> None:
    """Persist an already-minted refresh token."""
    payload = decode_refresh_token(refresh_token)
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid refresh token structure",
        )
    fid = payload.get("fid")
    await save_refresh_token(
        db,
        user_id=user.id,
        business_id=user.business_id,
        jti=str(jti),
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
        ip_address=ip_address,
        user_agent=user_agent,
        family_id=UUID(str(fid)) if fid else None,
        created_at=created_at,
    )


def _ensure_not_locked(user: User) -> None:
    """Raise 403 while the account is locked. Never writes (see _record_failed_login)."""
    if not user.is_locked or user.locked_until is None:
        return
    locked_until = user.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if locked_until > datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account locked. Try after {locked_until.isoformat()}",
        )


async def _record_failed_login(
    user: User,
    *,
    ip_address: str | None,
    user_agent: str | None,
    pin: bool = False,
) -> None:
    """Persist a failed attempt, then raise.

    Runs in its own session/transaction and commits *before* the HTTPException
    leaves the request, so the counter, the lock and the audit rows survive the
    request session's rollback. The counter is bumped with an atomic UPDATE so
    concurrent guesses cannot be lost. An expired lock is cleared first, so the
    first wrong guess after expiry starts a fresh count.
    """
    now = datetime.now(timezone.utc)
    lock_until = now + timedelta(minutes=settings.lockout_minutes)
    method = "pin" if pin else "password"

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE users
                       SET is_locked = FALSE, failed_login_attempts = 0, locked_until = NULL
                     WHERE id = :id AND is_locked
                       AND locked_until IS NOT NULL AND locked_until <= :now
                    """
                ),
                {"id": user.id, "now": now},
            )
            attempts = (
                await session.execute(
                    text(
                        """
                        UPDATE users
                           SET failed_login_attempts = failed_login_attempts + 1
                         WHERE id = :id
                     RETURNING failed_login_attempts
                        """
                    ),
                    {"id": user.id},
                )
            ).scalar_one()
            locked = attempts >= settings.max_login_attempts
            if locked:
                await session.execute(
                    text(
                        "UPDATE users SET is_locked = TRUE, locked_until = :until WHERE id = :id"
                    ),
                    {"id": user.id, "until": lock_until},
                )
            await log_auth_event(
                session,
                business_id=user.business_id,
                user_id=user.id,
                event="login_failed",
                record_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
                extra={"method": method},
            )
            if locked:
                await log_auth_event(
                    session,
                    business_id=user.business_id,
                    user_id=user.id,
                    event="account_locked",
                    record_id=user.id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )

    if locked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account locked after too many attempts",
        )
    if pin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid PIN",
        )
    remaining = settings.max_login_attempts - attempts
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Invalid credentials. {remaining} attempts remaining",
    )


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
    )


async def login_with_password(
    db: AsyncSession,
    phone: str,
    password: str,
    ip_address: str | None,
    user_agent: str | None,
    business_slug: str | None = None,
) -> tuple[User, str, str]:
    stmt = (
        select(User)
        .where(User.phone == phone, User.deleted_at.is_(None))
        .options(
            selectinload(User.business).options(
                selectinload(Business.business_type),
                selectinload(Business.config),
            ),
            selectinload(User.default_branch),
        )
    )
    if business_slug:
        business = await _find_business_by_slug(db, business_slug)
        if business is None:
            # Same response as a bad password: do not reveal which slugs exist.
            raise _invalid_credentials()
        stmt = stmt.where(User.business_id == business.id)
    elif settings.login_require_business_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="business_slug is required",
        )

    matches = list((await db.execute(stmt)).scalars().all())
    if len(matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This phone number is used by more than one business; business_slug is required",
        )
    user = matches[0] if matches else None
    if user is None or not user.password_hash:
        raise _invalid_credentials()

    _ensure_not_locked(user)

    if user.business is None or not user.business.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business account suspended",
        )
    if user.business.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business account suspended",
        )

    if not await asyncio.to_thread(verify_password, password, user.password_hash):
        await _record_failed_login(
            user, ip_address=ip_address, user_agent=user_agent, pin=False
        )

    user.failed_login_attempts = 0
    user.is_locked = False
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()

    access_token = create_access_token(_build_token_payload(user))
    refresh_token = await _issue_refresh_token(db, user, ip_address, user_agent)
    await purge_stale_tokens(db, user.id)

    await log_auth_event(
        db,
        business_id=user.business_id,
        user_id=user.id,
        event="login_success",
        record_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        extra={"method": "password"},
    )
    await db.commit()
    return user, access_token, refresh_token


async def _find_business_by_slug(
    db: AsyncSession, business_slug: str
) -> Business | None:
    result = await db.execute(
        select(Business)
        .join(
            BusinessConfig,
            BusinessConfig.business_id == Business.id,
        )
        .where(
            BusinessConfig.config_json.op("->>")("slug") == business_slug,
            BusinessConfig.deleted_at.is_(None),
            Business.deleted_at.is_(None),
            Business.is_active.is_(True),
        )
        .options(selectinload(Business.business_type))
    )
    return result.scalar_one_or_none()


async def login_with_pin(
    db: AsyncSession,
    business_slug: str,
    user_id: UUID,
    pin_code: str,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[User, str, str]:
    business = await _find_business_by_slug(db, business_slug)
    if business is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business not found",
        )

    result = await db.execute(
        select(User)
        .where(
            User.id == user_id,
            User.business_id == business.id,
            User.pin_hash.isnot(None),
            User.deleted_at.is_(None),
        )
        .options(
            selectinload(User.business).options(
                selectinload(Business.business_type),
                selectinload(Business.config),
            ),
            selectinload(User.default_branch),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid PIN",
        )

    _ensure_not_locked(user)

    if not await asyncio.to_thread(verify_pin, pin_code, user.pin_hash):
        await _record_failed_login(
            user, ip_address=ip_address, user_agent=user_agent, pin=True
        )

    user.failed_login_attempts = 0
    user.is_locked = False
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()

    access_token = create_pin_access_token(_build_token_payload(user))
    refresh_token = await _issue_refresh_token(db, user, ip_address, user_agent)
    await purge_stale_tokens(db, user.id)

    await log_auth_event(
        db,
        business_id=user.business_id,
        user_id=user.id,
        event="login_success",
        record_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        extra={"method": "pin"},
    )
    await db.commit()
    return user, access_token, refresh_token


async def _reject_refresh(
    db: AsyncSession,
    *,
    user_id: UUID,
    business_id: UUID,
    family_id: UUID | None,
    jti: str,
    revoke: bool = False,
    event: str | None = None,
) -> HTTPException:
    """Optionally revoke the family and commit that *before* the 401 is raised."""
    if revoke:
        await revoke_token_family(db, user_id, family_id, jti)
    if event:
        await log_auth_event(
            db,
            business_id=business_id,
            user_id=user_id,
            event=event,
            record_id=user_id,
        )
    await db.commit()
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_REVOKED_DETAIL,
    )


async def _replay_recent_rotation(
    db: AsyncSession, old: StoredRefreshToken
) -> tuple[str, str] | None:
    """Return the pair minted when ``old`` was rotated, if that was moments ago.

    Both tokens are rebuilt from the successor row (same claims, same
    ``issued_at``), so the replay is byte-identical to the original response.
    Returns ``None`` when the rotation is outside the grace window, when the
    successor has itself been used/revoked, or for legacy tokens without a family.
    """
    grace = settings.refresh_grace_seconds
    if grace <= 0 or old.revoked_at is None:
        return None
    revoked_at = old.revoked_at
    if revoked_at.tzinfo is None:
        revoked_at = revoked_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - revoked_at > timedelta(seconds=grace):
        return None

    successor = await get_family_successor(db, old)
    if (
        successor is None
        or successor.revoked_at is not None
        or successor.expires_at <= datetime.now(timezone.utc)
        # the successor must be the rotation of ``old`` (minted when old was revoked)
        or abs((successor.created_at - revoked_at).total_seconds()) > grace
    ):
        return None

    result = await db.execute(
        select(User).where(
            User.id == old.user_id,
            User.business_id == old.business_id,
            User.deleted_at.is_(None),
            User.status == "active",
        )
    )
    user = result.scalar_one_or_none()
    if user is None or user.is_locked:
        return None

    issued_at = successor.created_at
    refresh = create_refresh_token(
        {**_build_token_payload(user), "fid": str(old.family_id)},
        issued_at=issued_at,
        jti=successor.jti,
    )
    access = create_access_token(_build_token_payload(user), issued_at=issued_at)
    logger.info("refresh rotation replayed", extra={"user_id": str(user.id)})
    return access, refresh


async def refresh_tokens(
    db: AsyncSession,
    refresh_token: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str]:
    """Rotate: consume the presented refresh token, return (access, new_refresh).

    Presenting a token that was already rotated/revoked is treated as theft:
    the whole token family is revoked.
    """
    try:
        payload = decode_refresh_token(refresh_token)
        user_id = get_user_id_from_token(payload)
    except TokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        ) from None
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )
    jti = str(jti)

    stored = await get_refresh_token(db, jti)
    claimed_business = payload.get("business_id")
    if (
        stored is None
        or stored.user_id != user_id
        or not claimed_business
        or str(stored.business_id) != str(claimed_business)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_REVOKED_DETAIL
        )

    ctx = {
        "user_id": user_id,
        "business_id": stored.business_id,
        "family_id": stored.family_id,
        "jti": jti,
    }

    if stored.revoked_at is not None:
        replay = await _replay_recent_rotation(db, stored)
        if replay is not None:
            return replay
        logger.warning("refresh token reuse detected", extra={"user_id": str(user_id)})
        raise await _reject_refresh(
            db, **ctx, revoke=True, event="refresh_reuse_detected"
        )
    if stored.expires_at <= datetime.now(timezone.utc):
        raise await _reject_refresh(db, **ctx)

    # Atomic consume: of two concurrent presentations only one wins.
    if not await revoke_refresh_token(db, jti):
        # The loser of a double submit: the winner has committed by now, so its
        # new pair can be handed back rather than treating this as theft.
        fresh = await get_refresh_token(db, jti)
        if fresh is not None:
            replay = await _replay_recent_rotation(db, fresh)
            if replay is not None:
                return replay
        raise await _reject_refresh(
            db, **ctx, revoke=True, event="refresh_reuse_detected"
        )

    result = await db.execute(
        select(User)
        .where(
            User.id == user_id,
            User.business_id == stored.business_id,
            User.deleted_at.is_(None),
            User.status == "active",
        )
        .options(selectinload(User.business))
    )
    user = result.scalar_one_or_none()
    business = user.business if user is not None else None
    if (
        user is None
        or user.is_locked
        or business is None
        or not business.is_active
        or business.deleted_at is not None
    ):
        raise await _reject_refresh(db, **ctx, revoke=True)

    issued_at = datetime.now(timezone.utc)
    new_refresh = await _issue_refresh_token(
        db,
        user,
        ip_address,
        user_agent,
        family_id=stored.family_id,
        issued_at=issued_at,
    )
    new_access = create_access_token(_build_token_payload(user), issued_at=issued_at)
    await log_auth_event(
        db,
        business_id=user.business_id,
        user_id=user.id,
        event="token_refreshed",
        record_id=user.id,
    )
    await db.commit()
    return new_access, new_refresh


async def load_user_for_response(db: AsyncSession, user_id: UUID) -> User:
    result = await db.execute(
        select(User)
        .where(User.id == user_id, User.deleted_at.is_(None))
        .options(
            selectinload(User.business).options(
                selectinload(Business.business_type),
                selectinload(Business.config),
            ),
            selectinload(User.default_branch),
            selectinload(User.user_roles)
            .selectinload(UserRole.role)
            .selectinload(Role.role_permissions)
            .selectinload(RolePermission.permission),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


def build_user_info(user: User) -> UserInfo:
    business = user.business
    if business is None or business.business_type is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User business context not loaded",
        )
    return UserInfo(
        id=user.id,
        full_name=user.full_name,
        phone=user.phone or "",
        business_id=user.business_id,
        branch_id=user.default_branch_id,
        business_name=business.name,
        business_type_code=business.business_type.code,
        business_slug=business_slug_from_business(business),
        roles=user_roles_for_response(user),
        permission_keys=user_permission_keys_for_response(user),
        permissions_version=permissions_version(user),
    )


def build_subscription_summary(business) -> SubscriptionSummary:
    return SubscriptionSummary(**summary(compute_state(business)))


async def lease_for_business(db: AsyncSession, business_id: UUID) -> dict | None:
    business = await db.get(Business, business_id)
    return None if business is None else issue_lease(business)


async def subscription_summary_for_business(
    db: AsyncSession, business_id: UUID
) -> SubscriptionSummary | None:
    business = await db.get(Business, business_id)
    if business is None:
        return None
    return build_subscription_summary(business)


async def build_token_response(
    db: AsyncSession,
    user: User,
    access_token: str,
    refresh_token: str,
) -> TokenResponse:
    loaded = user
    if user.business is None or user.business.business_type is None:
        loaded = await load_user_for_response(db, user.id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user=build_user_info(loaded),
        subscription=build_subscription_summary(loaded.business),
        lease=issue_lease(loaded.business),
    )


async def register_business_and_authenticate(
    db: AsyncSession,
    data: RegisterBusinessRequest,
    ip_address: str | None,
    user_agent: str | None,
) -> TokenResponse:
    try:
        user, _business = await register_new_business(db, data)
    except IntegrityError:
        # Lost a race on the phone/slug uniqueness check.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Phone number already registered",
        ) from None
    loaded = await load_user_for_response(db, user.id)
    payload = _build_token_payload(loaded)
    access_token = create_access_token(payload)
    refresh_token = await _issue_refresh_token(db, loaded, ip_address, user_agent)
    await log_auth_event(
        db,
        business_id=loaded.business_id,
        user_id=loaded.id,
        event="login_success",
        record_id=loaded.id,
        ip_address=ip_address,
        user_agent=user_agent,
        extra={"method": "register"},
    )
    await db.commit()
    return await build_token_response(db, loaded, access_token, refresh_token)


async def login_password_and_build_response(
    db: AsyncSession,
    phone: str,
    password: str,
    ip_address: str | None,
    user_agent: str | None,
    business_slug: str | None = None,
) -> TokenResponse:
    user, access_token, refresh_token = await login_with_password(
        db, phone, password, ip_address, user_agent, business_slug
    )
    return await build_token_response(db, user, access_token, refresh_token)


async def login_pin_and_build_response(
    db: AsyncSession,
    business_slug: str,
    user_id: UUID,
    pin_code: str,
    ip_address: str | None,
    user_agent: str | None,
) -> TokenResponse:
    user, access_token, refresh_token = await login_with_pin(
        db, business_slug, user_id, pin_code, ip_address, user_agent
    )
    return await build_token_response(db, user, access_token, refresh_token)


async def logout_with_refresh_token(
    db: AsyncSession,
    user: User,
    refresh_token: str,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    try:
        payload = decode_refresh_token(refresh_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        ) from exc

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    if str(payload.get("sub")) != str(user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token does not belong to this user",
        )

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    # Ends the whole session (every rotated token descended from this login).
    stored = await get_refresh_token(db, str(jti))
    if stored is not None and stored.user_id == user.id:
        await revoke_token_family(db, user.id, stored.family_id, str(jti))
    await log_auth_event(
        db,
        business_id=user.business_id,
        user_id=user.id,
        event="logout",
        record_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await db.commit()
