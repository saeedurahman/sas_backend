"""Authentication flows: password login, PIN login, token refresh."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.business import Business, BusinessConfig
from app.models.user import User
from app.services.audit_helper import log_auth_event
from app.services.auth_service import (
    create_access_token,
    create_pin_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_jti_from_token,
    get_user_id_from_token,
    verify_password,
    verify_pin,
)
from app.schemas.auth import RegisterBusinessRequest, TokenResponse, UserInfo
from app.services.registration_service import register_new_business
from app.services.token_service import (
    is_refresh_token_valid,
    revoke_refresh_token,
    save_refresh_token,
)

BRANCH_LIMITS = {
    "trial": 1,
    "basic": 1,
    "growth": 3,
    "pro": 5,
}


def _build_token_payload(user: User) -> dict[str, str | None]:
    return {
        "sub": str(user.id),
        "business_id": str(user.business_id),
        "branch_id": str(user.default_branch_id) if user.default_branch_id else None,
    }


async def _save_refresh_from_token(
    db: AsyncSession,
    user: User,
    refresh_token: str,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    payload = decode_refresh_token(refresh_token)
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid refresh token structure",
        )
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    await save_refresh_token(
        db,
        user_id=user.id,
        business_id=user.business_id,
        jti=str(jti),
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )


async def _check_and_unlock_user(db: AsyncSession, user: User) -> None:
    now = datetime.now(timezone.utc)
    if not user.is_locked:
        return
    if user.locked_until is not None:
        locked_until = user.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account locked. Try after {locked_until.isoformat()}",
            )
    user.is_locked = False
    user.failed_login_attempts = 0
    user.locked_until = None
    await db.flush()


async def _record_failed_login(
    db: AsyncSession,
    user: User,
    *,
    ip_address: str | None,
    user_agent: str | None,
    pin: bool = False,
) -> None:
    user.failed_login_attempts += 1
    await log_auth_event(
        db,
        business_id=user.business_id,
        user_id=user.id,
        event="login_failed",
        record_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        extra={"method": "pin" if pin else "password"},
    )
    if user.failed_login_attempts >= settings.max_login_attempts:
        user.is_locked = True
        user.locked_until = datetime.now(timezone.utc) + timedelta(
            minutes=settings.lockout_minutes
        )
        await log_auth_event(
            db,
            business_id=user.business_id,
            user_id=user.id,
            event="account_locked",
            record_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account locked after too many attempts",
        )
    remaining = settings.max_login_attempts - user.failed_login_attempts
    await db.flush()
    if pin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid PIN",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Invalid credentials. {remaining} attempts remaining",
    )


async def login_with_password(
    db: AsyncSession,
    phone: str,
    password: str,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[User, str, str]:
    result = await db.execute(
        select(User)
        .where(User.phone == phone, User.deleted_at.is_(None))
        .options(
            selectinload(User.business).selectinload(Business.business_type),
            selectinload(User.default_branch),
        )
    )
    user = result.scalar_one_or_none()
    if user is None or not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    await _check_and_unlock_user(db, user)

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

    if not verify_password(password, user.password_hash):
        await _record_failed_login(
            db, user, ip_address=ip_address, user_agent=user_agent, pin=False
        )

    user.failed_login_attempts = 0
    user.is_locked = False
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()

    payload = _build_token_payload(user)
    access_token = create_access_token(payload)
    refresh_token = create_refresh_token(payload)
    await _save_refresh_from_token(db, user, refresh_token, ip_address, user_agent)

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
            selectinload(User.business).selectinload(Business.business_type),
            selectinload(User.default_branch),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid PIN",
        )

    await _check_and_unlock_user(db, user)

    if not verify_pin(pin_code, user.pin_hash):
        await _record_failed_login(
            db, user, ip_address=ip_address, user_agent=user_agent, pin=True
        )

    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()

    payload = _build_token_payload(user)
    access_token = create_pin_access_token(payload)
    refresh_token = create_refresh_token(payload)
    await _save_refresh_from_token(db, user, refresh_token, ip_address, user_agent)

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


async def refresh_access_token(db: AsyncSession, refresh_token: str) -> str:
    jti = get_jti_from_token(refresh_token)
    if not await is_refresh_token_valid(db, jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token revoked or expired",
        )

    payload = decode_refresh_token(refresh_token)
    try:
        user_id = get_user_id_from_token(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token revoked or expired",
        ) from exc

    business_id_raw = payload.get("business_id")
    if not business_id_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token revoked or expired",
        )
    business_id = UUID(str(business_id_raw))

    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.business_id == business_id,
            User.deleted_at.is_(None),
            User.status == "active",
        )
    )
    user = result.scalar_one_or_none()
    if user is None or user.is_locked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token revoked or expired",
        )

    new_access = create_access_token(_build_token_payload(user))
    await log_auth_event(
        db,
        business_id=user.business_id,
        user_id=user.id,
        event="token_refreshed",
        record_id=user.id,
    )
    await db.commit()
    return new_access


async def load_user_for_response(db: AsyncSession, user_id: UUID) -> User:
    result = await db.execute(
        select(User)
        .where(User.id == user_id, User.deleted_at.is_(None))
        .options(
            selectinload(User.business).selectinload(Business.business_type),
            selectinload(User.default_branch),
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
    )


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
    )


async def register_business_and_authenticate(
    db: AsyncSession,
    data: RegisterBusinessRequest,
    ip_address: str | None,
    user_agent: str | None,
) -> TokenResponse:
    user, _business = await register_new_business(db, data)
    loaded = await load_user_for_response(db, user.id)
    payload = _build_token_payload(loaded)
    access_token = create_access_token(payload)
    refresh_token = create_refresh_token(payload)
    await _save_refresh_from_token(db, loaded, refresh_token, ip_address, user_agent)
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
) -> TokenResponse:
    user, access_token, refresh_token = await login_with_password(
        db, phone, password, ip_address, user_agent
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

    await revoke_refresh_token(db, str(jti))
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
