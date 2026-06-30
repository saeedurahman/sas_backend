from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.database import get_db
from app.models.business import BusinessConfig
from app.models.user import Role, RolePermission, User, UserRole
from app.services.auth_service import TokenError, decode_token, get_user_id_from_token

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.api_v1_prefix}/auth/login",
    auto_error=True,
)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = decode_token(token)
        user_id = get_user_id_from_token(payload)
    except TokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    from app.models.business import Business

    stmt = (
        select(User)
        .where(User.id == user_id)
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
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    if user.is_locked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is locked",
        )

    if user.business is None or not user.business.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business account is inactive",
        )

    if user.business.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business account is inactive",
        )

    return user


def _user_role_names(user: User) -> set[str]:
    names: set[str] = set()
    for user_role in user.user_roles:
        if user_role.deleted_at is not None:
            continue
        if user_role.role is not None and user_role.role.deleted_at is None:
            names.add(user_role.role.name.lower())
    return names


async def require_owner(
    current_user: User = Depends(get_current_user),
) -> User:
    if "owner" not in _user_role_names(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner role required",
        )
    return current_user


async def require_manager(
    current_user: User = Depends(get_current_user),
) -> User:
    roles = _user_role_names(current_user)
    if not roles.intersection({"owner", "manager"}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manager or owner role required",
        )
    return current_user


def _user_permission_keys(user: User) -> set[str]:
    keys: set[str] = set()
    business_id = user.business_id
    for user_role in user.user_roles:
        if user_role.deleted_at is not None or user_role.role is None:
            continue
        if user_role.business_id != business_id:
            continue
        role = user_role.role
        if role.deleted_at is not None or role.business_id != business_id:
            continue
        for role_perm in role.role_permissions:
            if role_perm.business_id != business_id:
                continue
            if role_perm.permission is not None:
                keys.add(role_perm.permission.permission_key)
    return keys


def user_roles_for_response(user: User) -> list[str]:
    """Role display names for the current business, sorted case-insensitively."""
    names: list[str] = []
    business_id = user.business_id
    for user_role in user.user_roles:
        if user_role.deleted_at is not None or user_role.role is None:
            continue
        if user_role.business_id != business_id:
            continue
        role = user_role.role
        if role.deleted_at is not None or role.business_id != business_id:
            continue
        names.append(role.name)
    return sorted(names, key=str.lower)


def user_permission_keys_for_response(user: User) -> list[str]:
    return sorted(_user_permission_keys(user))


def require_permission(permission_key: str) -> Callable[..., User]:
    async def _checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if permission_key not in _user_permission_keys(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {permission_key}",
            )
        return current_user

    return _checker


def require_any_permission(*permission_keys: str) -> Callable[..., User]:
    if not permission_keys:
        raise ValueError("require_any_permission requires at least one permission key")

    async def _checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if not _user_permission_keys(current_user).intersection(permission_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of these permissions required: {', '.join(permission_keys)}",
            )
        return current_user

    return _checker


async def get_business_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BusinessConfig:
    stmt = select(BusinessConfig).where(
        BusinessConfig.business_id == current_user.business_id,
        BusinessConfig.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business configuration not found",
        )
    return config
