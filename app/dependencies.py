from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload, selectinload

from app.core import permission_cache
from app.core.config import settings
from app.database import get_db
from app.models.business import BusinessConfig
from app.models.user import Role, RolePermission, User, UserRole
from app.services.auth_service import TokenError, decode_token, get_user_id_from_token

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.api_v1_prefix}/auth/login",
    auto_error=True,
)


async def subscription_exempt(request: Request) -> None:
    """Whitelist: this route works for expired/suspended/grace businesses.

    Applied as a route/router dependency (it runs before ``get_current_user``).
    """
    request.state.subscription_exempt = True


async def allow_in_grace(request: Request) -> None:
    """Whitelist: writes are allowed during the read-only grace period (not after it)."""
    request.state.grace_write_ok = True


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def _enforce_subscription(request: Request, business, db: AsyncSession) -> None:
    from app.services import subscription as sub

    if getattr(request.state, "subscription_exempt", False):
        return
    state = sub.compute_state(business)
    if state.status in (sub.ACTIVE, sub.TRIAL):
        return
    if state.status == sub.GRACE:
        if request.method in _READ_METHODS or getattr(
            request.state, "grace_write_ok", False
        ):
            return
        raise await sub.build_blocked(db, state, business, grace_write=True)
    raise await sub.build_blocked(db, state, business, grace_write=False)


async def get_current_user(
    request: Request,
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

    cached_keys = permission_cache.get(user_id)
    roles_load = selectinload(User.user_roles).selectinload(UserRole.role)
    if cached_keys is None:
        roles_load = roles_load.selectinload(Role.role_permissions).selectinload(
            RolePermission.permission
        )
    else:
        # Permissions are known; skip the two heaviest queries.
        roles_load = roles_load.options(noload(Role.role_permissions))

    stmt = (
        select(User)
        .where(User.id == user_id)
        .options(
            selectinload(User.business).options(
                selectinload(Business.business_type),
                selectinload(Business.config),
            ),
            selectinload(User.default_branch),
            roles_load,
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

    # Password/PIN change, reset and deactivation bump token_version; tokens
    # minted before that (older "tv", or none = 0) are no longer accepted.
    if int(payload.get("tv", 0)) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is no longer valid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Uses the business row loaded above: no extra query on the happy path.
    await _enforce_subscription(request, user.business, db)

    if cached_keys is not None:
        user._permission_keys = cached_keys
    else:
        user._permission_keys = frozenset(_compute_permission_keys(user))
        permission_cache.put(user.id, user._permission_keys)

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


# Markers read by scripts/route_permission_report.py (CI): a route must depend on one.
require_owner.__route_guard__ = "owner"  # type: ignore[attr-defined]
require_manager.__route_guard__ = "manager"  # type: ignore[attr-defined]


def _user_permission_keys(user: User) -> set[str]:
    cached = getattr(user, "_permission_keys", None)
    if cached is not None:
        return set(cached)
    return _compute_permission_keys(user)


def _compute_permission_keys(user: User) -> set[str]:
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


def allowed_branch_ids(user: User) -> frozenset[UUID] | None:
    """Branches this user may touch; ``None`` means every branch of the business.

    * Owners see everything.
    * ``user_roles.branch_id`` is honoured: a role scoped to a branch grants access to
      that branch only.
    * A user with an assigned branch (``default_branch_id``) is limited to it (plus any
      scoped-role branches), even when their roles are not branch-scoped.
    * A user with no assigned branch and an unscoped role is business-wide.
    """
    scoped: set[UUID] = set()
    unscoped = False
    for user_role in user.user_roles:
        if user_role.deleted_at is not None or user_role.role is None:
            continue
        if user_role.role.deleted_at is not None:
            continue
        if user_role.business_id != user.business_id:
            continue
        if user_role.role.name.lower() == "owner":
            return None
        if user_role.branch_id is None:
            unscoped = True
        else:
            scoped.add(user_role.branch_id)
    if user.default_branch_id is not None:
        scoped.add(user.default_branch_id)
        return frozenset(scoped)
    if unscoped or not scoped:
        return None
    return frozenset(scoped)


def user_can_access_branch(user: User, branch_id: UUID) -> bool:
    allowed = allowed_branch_ids(user)
    return allowed is None or branch_id in allowed


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


def permissions_version(user: User) -> str:
    """Stable fingerprint of what this user may do (roles, permission keys, branch scope).

    Content-derived, so it is identical across workers/restarts and changes exactly when
    the effective access changes. Clients compare it with the value they stored and
    re-fetch /auth/me when it differs.
    """
    import hashlib
    import json

    branches = allowed_branch_ids(user)
    material = json.dumps(
        {
            "roles": sorted(r.lower() for r in user_roles_for_response(user)),
            "keys": sorted(_user_permission_keys(user)),
            "branches": None if branches is None else sorted(str(b) for b in branches),
        },
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


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

    _checker.__route_guard__ = f"permission:{permission_key}"  # type: ignore[attr-defined]
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

    _checker.__route_guard__ = "permission:any"  # type: ignore[attr-defined]
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


def require_feature_flags(*flag_keys: str) -> Callable[..., BusinessConfig]:
    """Return 403 when any config_json feature flag is disabled."""

    async def _checker(
        config: BusinessConfig = Depends(get_business_config),
    ) -> BusinessConfig:
        from app.services.feature_flags import get_feature_flag

        for flag_key in flag_keys:
            if not get_feature_flag(config.config_json, flag_key):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Feature not enabled: {flag_key}",
                )
        return config

    return _checker


def require_feature_flag(flag_key: str) -> Callable[..., BusinessConfig]:
    """Return 403 when a config_json feature flag is disabled for the tenant."""

    async def _checker(
        config: BusinessConfig = Depends(get_business_config),
    ) -> BusinessConfig:
        from app.services.feature_flags import get_feature_flag

        if not get_feature_flag(config.config_json, flag_key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature not enabled: {flag_key}",
            )
        return config

    return _checker


async def apply_business_timezone(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Make date casts and CURRENT_DATE in this request follow the business timezone.

    Router-level dependency for reporting endpoints: ``sold_at::date`` and
    "today" must mean the business's day (default Asia/Karachi), not the
    server's. ``set_config(..., true)`` lasts for the request's transaction.
    """
    from app.services.business_time import get_business_tz

    zone = await get_business_tz(db, current_user.business_id)
    await db.execute(
        text("SELECT set_config('TimeZone', :tz, true)"), {"tz": zone.key}
    )
