"""Tenant user management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import Role, User, UserRole
from app.schemas.user import CreateUserRequest, UpdateUserRequest
from app.services.audit_helper import log_auth_event
from app.services.auth_service import hash_password, hash_pin, verify_password, verify_pin
from app.services.token_service import revoke_all_user_tokens


def _is_owner(user: User) -> bool:
    for user_role in user.user_roles:
        if user_role.deleted_at is not None or user_role.role is None:
            continue
        if user_role.role.deleted_at is None and user_role.role.name.lower() == "owner":
            return True
    return False


async def _count_active_owners(db: AsyncSession, business_id: UUID) -> int:
    owners_result = await db.execute(
        select(User)
        .where(User.business_id == business_id, User.deleted_at.is_(None))
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
    )
    count = 0
    for user in owners_result.scalars().unique().all():
        if _is_owner(user):
            count += 1
    return count


async def _resolve_business_roles(
    db: AsyncSession,
    business_id: UUID,
    role_ids: list[UUID],
) -> list[Role]:
    unique_ids = list(dict.fromkeys(role_ids))
    result = await db.execute(
        select(Role).where(
            Role.business_id == business_id,
            Role.id.in_(unique_ids),
            Role.deleted_at.is_(None),
        )
    )
    roles = list(result.scalars().all())
    if len(roles) != len(unique_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more roles not found for this business",
        )
    return roles


async def _load_user_role_assignments(
    db: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID,
) -> list[UserRole]:
    result = await db.execute(
        select(UserRole).where(
            UserRole.business_id == business_id,
            UserRole.user_id == user_id,
        )
    )
    return list(result.scalars().all())


async def _replace_user_roles(
    db: AsyncSession,
    user: User,
    roles: list[Role],
    *,
    updated_by: UUID,
) -> None:
    business_id = user.business_id
    new_role_ids = {role.id for role in roles}
    branch_id = user.default_branch_id
    now = datetime.now(timezone.utc)

    assignments = await _load_user_role_assignments(
        db, business_id=business_id, user_id=user.id
    )
    by_role_id: dict[UUID, list[UserRole]] = {}
    for assignment in assignments:
        by_role_id.setdefault(assignment.role_id, []).append(assignment)

    for assignment in assignments:
        if assignment.deleted_at is None and assignment.role_id not in new_role_ids:
            assignment.deleted_at = now
            assignment.updated_at = now

    for role in roles:
        existing = by_role_id.get(role.id, [])
        active = next((a for a in existing if a.deleted_at is None), None)
        if active is not None:
            continue

        reactivate = next(
            (
                a
                for a in existing
                if a.deleted_at is not None and a.branch_id == branch_id
            ),
            None,
        )
        if reactivate is not None:
            reactivate.deleted_at = None
            reactivate.updated_at = now
            continue

        db.add(
            UserRole(
                business_id=business_id,
                user_id=user.id,
                role_id=role.id,
                branch_id=branch_id,
                created_by=updated_by,
                created_at=now,
                updated_at=now,
            )
        )

    await db.flush()


async def replace_user_roles(
    db: AsyncSession,
    user_id: UUID,
    business_id: UUID,
    role_ids: list[UUID],
    updated_by: UUID,
) -> User:
    user = await get_user_by_id(db, user_id, business_id)
    roles = await _resolve_business_roles(db, business_id, role_ids)

    owner_role = next((r for r in roles if r.name.lower() == "owner"), None)
    if _is_owner(user) and owner_role is None:
        if await _count_active_owners(db, business_id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove the owner role from the last owner of the business",
            )

    now = datetime.now(timezone.utc)
    await _replace_user_roles(db, user, roles, updated_by=updated_by)
    user.updated_by = updated_by
    user.updated_at = now
    await db.commit()
    return await get_user_by_id(db, user_id, business_id)


async def get_users_for_business(
    db: AsyncSession,
    business_id: UUID,
    role_filter: str | None = None,
) -> list[User]:
    stmt = (
        select(User)
        .where(User.business_id == business_id, User.deleted_at.is_(None))
        .options(
            selectinload(User.user_roles).selectinload(UserRole.role),
        )
        .order_by(User.full_name)
    )
    result = await db.execute(stmt)
    users = list(result.scalars().unique().all())

    if role_filter is None:
        return users

    role_lower = role_filter.lower()
    filtered: list[User] = []
    for user in users:
        for user_role in user.user_roles:
            if user_role.deleted_at is not None or user_role.role is None:
                continue
            if (
                user_role.role.deleted_at is None
                and user_role.role.name.lower() == role_lower
            ):
                filtered.append(user)
                break
    return filtered


def _phone_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Phone number already registered",
    )


async def create_tenant_user(
    db: AsyncSession,
    business_id: UUID,
    data: CreateUserRequest,
    creator_id: UUID,
) -> User:
    phone_check = await db.execute(
        select(User.id).where(User.phone == data.phone, User.deleted_at.is_(None))
    )
    if phone_check.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Phone number already registered",
        )

    now = datetime.now(timezone.utc)
    user = User(
        business_id=business_id,
        default_branch_id=data.branch_id,
        full_name=data.full_name,
        phone=data.phone,
        password_hash=hash_password(data.password),
        status="active",
        is_locked=False,
        failed_login_attempts=0,
        created_by=creator_id,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _phone_conflict() from None

    if data.role_ids:
        roles_result = await db.execute(
            select(Role).where(
                Role.business_id == business_id,
                Role.id.in_(data.role_ids),
                Role.deleted_at.is_(None),
            )
        )
        roles = list(roles_result.scalars().all())
        for role in roles:
            db.add(
                UserRole(
                    business_id=business_id,
                    user_id=user.id,
                    role_id=role.id,
                    branch_id=data.branch_id,
                    created_by=creator_id,
                    created_at=now,
                    updated_at=now,
                )
            )

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _phone_conflict() from None
    return await get_user_by_id(db, user.id, business_id)


async def get_user_by_id(
    db: AsyncSession,
    user_id: UUID,
    business_id: UUID,
) -> User:
    result = await db.execute(
        select(User)
        .where(User.id == user_id, User.deleted_at.is_(None))
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    if user.business_id != business_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    return user


async def update_tenant_user(
    db: AsyncSession,
    user_id: UUID,
    business_id: UUID,
    data: UpdateUserRequest,
    updated_by: UUID,
) -> User:
    user = await get_user_by_id(db, user_id, business_id)
    now = datetime.now(timezone.utc)

    if data.full_name is not None:
        user.full_name = data.full_name
    if data.branch_id is not None:
        user.default_branch_id = data.branch_id
    if data.is_active is not None:
        user.status = "active" if data.is_active else "inactive"

    user.updated_by = updated_by
    user.updated_at = now
    await db.commit()
    return await get_user_by_id(db, user_id, business_id)


def _verify_current_secret(
    user: User,
    *,
    current_password: str | None,
    current_pin: str | None,
) -> None:
    """Require proof of a current credential before a self-service change."""
    if current_password is not None and user.password_hash:
        if verify_password(current_password, user.password_hash):
            return
    elif current_pin is not None and user.pin_hash:
        if verify_pin(current_pin, user.pin_hash):
            return
    elif current_password is None and current_pin is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current_password or current_pin is required",
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Current password or PIN is incorrect",
    )


async def set_user_pin(
    db: AsyncSession,
    target_user_id: UUID,
    business_id: UUID,
    pin_code: str,
    requesting_user: User,
    current_password: str | None = None,
    current_pin: str | None = None,
) -> None:
    target = await get_user_by_id(db, target_user_id, business_id)

    is_self = requesting_user.id == target_user_id
    if not is_self and not _is_owner(requesting_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not allowed to set PIN for this user",
        )
    if is_self:
        _verify_current_secret(
            target, current_password=current_password, current_pin=current_pin
        )

    now = datetime.now(timezone.utc)
    target.pin_hash = hash_pin(pin_code)
    target.updated_by = requesting_user.id
    target.updated_at = now
    await revoke_all_user_tokens(db, target.id)
    await log_auth_event(
        db,
        business_id=business_id,
        user_id=requesting_user.id,
        event="pin_changed" if is_self else "pin_reset",
        record_id=target.id,
    )
    await db.commit()


async def change_own_password(
    db: AsyncSession,
    user: User,
    current_password: str,
    new_password: str,
) -> None:
    if not user.password_hash or not verify_password(current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect",
        )
    if verify_password(new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current password",
        )
    now = datetime.now(timezone.utc)
    user.password_hash = hash_password(new_password)
    user.updated_by = user.id
    user.updated_at = now
    await revoke_all_user_tokens(db, user.id)
    await log_auth_event(
        db,
        business_id=user.business_id,
        user_id=user.id,
        event="password_changed",
        record_id=user.id,
    )
    await db.commit()


async def reset_user_password(
    db: AsyncSession,
    target_user_id: UUID,
    business_id: UUID,
    new_password: str,
    requesting_user: User,
) -> None:
    """Admin reset of another user's password (the forgot-password path for staff)."""
    if target_user_id == requesting_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use change-password to change your own password",
        )
    target = await get_user_by_id(db, target_user_id, business_id)
    if _is_owner(target) and not _is_owner(requesting_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can reset an owner's password",
        )
    now = datetime.now(timezone.utc)
    target.password_hash = hash_password(new_password)
    target.is_locked = False
    target.failed_login_attempts = 0
    target.locked_until = None
    target.updated_by = requesting_user.id
    target.updated_at = now
    await revoke_all_user_tokens(db, target.id)
    await log_auth_event(
        db,
        business_id=business_id,
        user_id=requesting_user.id,
        event="password_reset",
        record_id=target.id,
    )
    await db.commit()


async def soft_delete_tenant_user(
    db: AsyncSession,
    user_id: UUID,
    business_id: UUID,
    current_user_id: UUID,
) -> None:
    if user_id == current_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    user = await get_user_by_id(db, user_id, business_id)
    now = datetime.now(timezone.utc)

    if _is_owner(user) and await _count_active_owners(db, business_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the last owner of the business",
        )

    user.deleted_at = now
    user.deleted_by = current_user_id
    user.status = "inactive"
    user.updated_at = now
    await revoke_all_user_tokens(db, user_id)
    await db.commit()
