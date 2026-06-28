"""Role and permission assignment services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import Permission, Role, RolePermission, UserRole
from app.schemas.role import (
    AssignPermissionsRequest,
    CreateRoleRequest,
    PermissionItemResponse,
    PermissionModuleGroup,
    PermissionsCatalogResponse,
    RoleResponse,
    UpdateRoleRequest,
)
from app.services.role_permission_seed import permission_keys_for_role


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _role_detail_options():
    return (
        selectinload(Role.role_permissions).selectinload(RolePermission.permission),
    )


def _permission_items_from_role_permissions(
    role_permissions: list[RolePermission],
) -> tuple[list[str], list[PermissionItemResponse]]:
    items: list[PermissionItemResponse] = []
    keys: list[str] = []
    for role_perm in role_permissions:
        if role_perm.permission is None:
            continue
        perm = role_perm.permission
        keys.append(perm.permission_key)
        items.append(
            PermissionItemResponse(
                permission_key=perm.permission_key,
                description=perm.description,
                module=perm.module,
            )
        )
    keys.sort()
    items.sort(key=lambda p: p.permission_key)
    return keys, items


def role_to_response(role: Role) -> RoleResponse:
    permission_keys, permissions = _permission_items_from_role_permissions(
        role.role_permissions
    )
    return RoleResponse(
        id=role.id,
        business_id=role.business_id,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        permission_keys=permission_keys,
        permissions=permissions,
        created_at=role.created_at,
        updated_at=role.updated_at,
        deleted_at=role.deleted_at,
    )


async def get_permissions_catalog(db: AsyncSession) -> PermissionsCatalogResponse:
    result = await db.execute(
        select(Permission).order_by(Permission.module, Permission.permission_key)
    )
    permissions = list(result.scalars().all())

    grouped: dict[str, list[PermissionItemResponse]] = {}
    for perm in permissions:
        grouped.setdefault(perm.module, []).append(
            PermissionItemResponse(
                permission_key=perm.permission_key,
                description=perm.description,
                module=perm.module,
            )
        )

    modules = [
        PermissionModuleGroup(module=module_name, permissions=items)
        for module_name, items in sorted(grouped.items())
    ]
    return PermissionsCatalogResponse(modules=modules)


async def _resolve_permissions_by_keys(
    db: AsyncSession,
    permission_keys: list[str],
) -> dict[str, Permission]:
    if not permission_keys:
        return {}

    unique_keys = sorted(set(permission_keys))
    result = await db.execute(
        select(Permission).where(Permission.permission_key.in_(unique_keys))
    )
    permissions = list(result.scalars().all())
    found = {perm.permission_key: perm for perm in permissions}
    missing = sorted(set(unique_keys) - set(found.keys()))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permission keys: {', '.join(missing)}",
        )
    return found


async def _required_owner_permission_keys(db: AsyncSession) -> set[str]:
    candidate_keys = permission_keys_for_role("owner")
    result = await db.execute(
        select(Permission.permission_key).where(
            Permission.permission_key.in_(sorted(candidate_keys))
        )
    )
    return set(result.scalars().all())


async def _ensure_unique_role_name(
    db: AsyncSession,
    business_id: UUID,
    name: str,
    *,
    exclude_role_id: UUID | None = None,
) -> None:
    stmt = select(Role.id).where(
        Role.business_id == business_id,
        func.lower(Role.name) == name.lower(),
        Role.deleted_at.is_(None),
    )
    if exclude_role_id is not None:
        stmt = stmt.where(Role.id != exclude_role_id)

    conflict = (await db.execute(stmt)).scalar_one_or_none()
    if conflict is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Role name '{name}' is already in use",
        )


async def get_role_by_id(
    db: AsyncSession,
    role_id: UUID,
    business_id: UUID,
) -> Role:
    result = await db.execute(
        select(Role)
        .where(
            Role.id == role_id,
            Role.business_id == business_id,
            Role.deleted_at.is_(None),
        )
        .options(*_role_detail_options())
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        )
    return role


async def list_roles_for_business(
    db: AsyncSession,
    business_id: UUID,
) -> list[RoleResponse]:
    result = await db.execute(
        select(Role)
        .where(
            Role.business_id == business_id,
            Role.deleted_at.is_(None),
        )
        .options(*_role_detail_options())
        .order_by(Role.is_system.desc(), Role.name)
    )
    roles = list(result.scalars().unique().all())
    return [role_to_response(role) for role in roles]


async def get_role_response(
    db: AsyncSession,
    role_id: UUID,
    business_id: UUID,
) -> RoleResponse:
    role = await get_role_by_id(db, role_id, business_id)
    return role_to_response(role)


async def _load_role_permissions(
    db: AsyncSession,
    role: Role,
) -> list[RolePermission]:
    result = await db.execute(
        select(RolePermission)
        .where(
            RolePermission.role_id == role.id,
            RolePermission.business_id == role.business_id,
        )
        .options(selectinload(RolePermission.permission))
    )
    return list(result.scalars().all())


async def _replace_role_permissions(
    db: AsyncSession,
    role: Role,
    permission_keys: list[str],
    *,
    created_by: UUID,
) -> None:
    if role.is_system and role.name.lower() == "owner":
        required_keys = await _required_owner_permission_keys(db)
        new_keys = set(permission_keys)
        stripped = sorted(required_keys - new_keys)
        if stripped:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "The owner role must retain all permissions; "
                    f"cannot remove: {', '.join(stripped)}"
                ),
            )

    permissions_by_key = await _resolve_permissions_by_keys(db, permission_keys)
    new_key_set = set(permissions_by_key.keys())

    current_role_perms = await _load_role_permissions(db, role)
    current_by_key: dict[str, RolePermission] = {}
    for role_perm in current_role_perms:
        if role_perm.permission is None:
            continue
        current_by_key[role_perm.permission.permission_key] = role_perm

    for key, role_perm in current_by_key.items():
        if key not in new_key_set:
            await db.delete(role_perm)

    for key in sorted(new_key_set):
        if key in current_by_key:
            continue
        permission = permissions_by_key[key]
        db.add(
            RolePermission(
                business_id=role.business_id,
                role_id=role.id,
                permission_id=permission.id,
                created_by=created_by,
            )
        )

    await db.flush()


async def create_role(
    db: AsyncSession,
    business_id: UUID,
    data: CreateRoleRequest,
    created_by: UUID,
) -> RoleResponse:
    await _ensure_unique_role_name(db, business_id, data.name)

    now = _now()
    role = Role(
        business_id=business_id,
        name=data.name.strip(),
        description=data.description,
        is_system=False,
        created_by=created_by,
        updated_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(role)
    await db.flush()

    if data.permission_keys:
        await _replace_role_permissions(
            db,
            role,
            data.permission_keys,
            created_by=created_by,
        )

    await db.commit()
    return await get_role_response(db, role.id, business_id)


async def update_role(
    db: AsyncSession,
    role_id: UUID,
    business_id: UUID,
    data: UpdateRoleRequest,
    updated_by: UUID,
) -> RoleResponse:
    role = await get_role_by_id(db, role_id, business_id)
    now = _now()

    if data.name is not None and data.name.strip().lower() != role.name.lower():
        if role.is_system:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "System roles (owner, manager, cashier) cannot be renamed "
                    "because authorization checks depend on role name. "
                    "You may update the description or adjust permissions instead."
                ),
            )
        await _ensure_unique_role_name(
            db, business_id, data.name, exclude_role_id=role.id
        )
        role.name = data.name.strip()

    if data.description is not None:
        role.description = data.description

    role.updated_by = updated_by
    role.updated_at = now
    await db.commit()
    return await get_role_response(db, role_id, business_id)


async def delete_role(
    db: AsyncSession,
    role_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    role = await get_role_by_id(db, role_id, business_id)

    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="System roles cannot be deleted",
        )

    assigned_count = (
        await db.execute(
            select(func.count())
            .select_from(UserRole)
            .where(
                UserRole.role_id == role_id,
                UserRole.business_id == business_id,
                UserRole.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    if assigned_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete role: {assigned_count} user(s) still assigned. "
                "Reassign them first."
            ),
        )

    now = _now()
    role.deleted_at = now
    role.deleted_by = deleted_by
    role.updated_at = now
    role.updated_by = deleted_by
    await db.commit()


async def replace_role_permissions(
    db: AsyncSession,
    role_id: UUID,
    business_id: UUID,
    data: AssignPermissionsRequest,
    updated_by: UUID,
) -> RoleResponse:
    role = await get_role_by_id(db, role_id, business_id)
    await _replace_role_permissions(
        db,
        role,
        data.permission_keys,
        created_by=updated_by,
    )
    role.updated_by = updated_by
    role.updated_at = _now()
    await db.commit()
    return await get_role_response(db, role_id, business_id)
