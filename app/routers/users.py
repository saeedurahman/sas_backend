from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.database import get_db
from app.dependencies import get_current_user, require_permission
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.user import (
    AssignUserRolesRequest,
    CreateUserRequest,
    ResetUserPasswordRequest,
    SetPinRequest,
    UpdateUserRequest,
    UserResponse,
)
from app.services.user_service import (
    create_tenant_user,
    get_user_by_id,
    get_users_for_business,
    replace_user_roles,
    reset_user_password,
    set_user_pin,
    soft_delete_tenant_user,
    update_tenant_user,
)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserResponse], status_code=status.HTTP_200_OK)
async def list_users(
    role: str | None = Query(default=None),
    current_user: User = Depends(require_permission("users.view")),
    db=Depends(get_db),
):
    return await get_users_for_business(db, current_user.business_id, role)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    data: CreateUserRequest,
    current_user: User = Depends(require_permission("users.create")),
    db=Depends(get_db),
):
    return await create_tenant_user(
        db, current_user.business_id, data, current_user.id
    )


@router.get("/{user_id}", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def get_user(
    user_id: UUID,
    current_user: User = Depends(require_permission("users.view")),
    db=Depends(get_db),
):
    return await get_user_by_id(db, user_id, current_user.business_id)


@router.put("/{user_id}", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def update_user(
    user_id: UUID,
    data: UpdateUserRequest,
    current_user: User = Depends(require_permission("users.update")),
    db=Depends(get_db),
):
    return await update_tenant_user(
        db, user_id, current_user.business_id, data, current_user.id
    )


@router.put(
    "/{user_id}/roles",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
)
async def replace_user_roles_endpoint(
    user_id: UUID,
    data: AssignUserRolesRequest,
    current_user: User = Depends(require_permission("users.roles.manage")),
    db=Depends(get_db),
):
    return await replace_user_roles(
        db,
        user_id,
        current_user.business_id,
        data.role_ids,
        current_user.id,
    )


@router.put(
    "/{user_id}/pin",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def update_user_pin(
    user_id: UUID,
    data: SetPinRequest,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    enforce_rate_limit(
        "pin-change", str(current_user.id), settings.rate_limit_pin_change_user
    )
    await set_user_pin(
        db,
        user_id,
        current_user.business_id,
        data.pin_code,
        current_user,
        current_password=data.current_password,
        current_pin=data.current_pin,
    )
    return MessageResponse(message="PIN updated successfully")


@router.put(
    "/{user_id}/password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def reset_password(
    user_id: UUID,
    data: ResetUserPasswordRequest,
    current_user: User = Depends(require_permission("users.update")),
    db=Depends(get_db),
):
    """Admin reset of a staff member's password (signs them out everywhere)."""
    enforce_rate_limit(
        "password-reset",
        str(current_user.id),
        settings.rate_limit_password_change_user,
    )
    await reset_user_password(
        db, user_id, current_user.business_id, data.new_password, current_user
    )
    return MessageResponse(message="Password reset")


@router.delete(
    "/{user_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_user(
    user_id: UUID,
    current_user: User = Depends(require_permission("users.delete")),
    db=Depends(get_db),
):
    await soft_delete_tenant_user(
        db, user_id, current_user.business_id, current_user.id
    )
    return MessageResponse(message="User deleted")
