"""Tenant user management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import Role, User, UserRole
from app.schemas.user import CreateUserRequest, UpdateUserRequest
from app.services.auth_service import hash_password, hash_pin
from app.services.token_service import revoke_all_user_tokens


def _is_owner(user: User) -> bool:
    for user_role in user.user_roles:
        if user_role.deleted_at is not None or user_role.role is None:
            continue
        if user_role.role.deleted_at is None and user_role.role.name.lower() == "owner":
            return True
    return False


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
    await db.flush()

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

    await db.commit()
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


async def set_user_pin(
    db: AsyncSession,
    target_user_id: UUID,
    business_id: UUID,
    pin_code: str,
    requesting_user: User,
) -> None:
    target = await get_user_by_id(db, target_user_id, business_id)

    is_self = requesting_user.id == target_user_id
    if not is_self and not _is_owner(requesting_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not allowed to set PIN for this user",
        )

    now = datetime.now(timezone.utc)
    target.pin_hash = hash_pin(pin_code)
    target.updated_by = requesting_user.id
    target.updated_at = now
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

    owners_result = await db.execute(
        select(User)
        .where(User.business_id == business_id, User.deleted_at.is_(None))
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
    )
    active_owners = 0
    for u in owners_result.scalars().unique().all():
        if _is_owner(u):
            active_owners += 1

    if _is_owner(user) and active_owners <= 1:
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
