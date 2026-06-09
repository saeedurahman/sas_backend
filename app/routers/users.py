from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager, require_owner
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.user import (
    CreateUserRequest,
    SetPinRequest,
    UpdateUserRequest,
    UserResponse,
)
from app.services.user_service import (
    create_tenant_user,
    get_user_by_id,
    get_users_for_business,
    set_user_pin,
    soft_delete_tenant_user,
    update_tenant_user,
)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserResponse], status_code=status.HTTP_200_OK)
async def list_users(
    role: str | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_users_for_business(db, current_user.business_id, role)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    data: CreateUserRequest,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await create_tenant_user(
        db, current_user.business_id, data, current_user.id
    )


@router.get("/{user_id}", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def get_user(
    user_id: UUID,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_user_by_id(db, user_id, current_user.business_id)


@router.put("/{user_id}", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def update_user(
    user_id: UUID,
    data: UpdateUserRequest,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await update_tenant_user(
        db, user_id, current_user.business_id, data, current_user.id
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
    await set_user_pin(
        db,
        user_id,
        current_user.business_id,
        data.pin_code,
        current_user,
    )
    return MessageResponse(message="PIN updated successfully")


@router.delete(
    "/{user_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_user(
    user_id: UUID,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    await soft_delete_tenant_user(
        db, user_id, current_user.business_id, current_user.id
    )
    return MessageResponse(message="User deleted")
