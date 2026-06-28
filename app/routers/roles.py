from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.dependencies import get_current_user, require_permission
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.role import (
    AssignPermissionsRequest,
    CreateRoleRequest,
    RoleResponse,
    UpdateRoleRequest,
)
from app.services.role_service import (
    create_role,
    delete_role,
    get_role_response,
    list_roles_for_business,
    replace_role_permissions,
    update_role,
)

router = APIRouter(prefix="/roles", tags=["Roles"])


@router.get("", response_model=list[RoleResponse], status_code=status.HTTP_200_OK)
async def list_roles(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await list_roles_for_business(db, current_user.business_id)


@router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role_endpoint(
    data: CreateRoleRequest,
    current_user: User = Depends(require_permission("users.roles.manage")),
    db=Depends(get_db),
):
    return await create_role(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{role_id}",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
)
async def get_role(
    role_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_role_response(db, role_id, current_user.business_id)


@router.put(
    "/{role_id}",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
)
async def update_role_endpoint(
    role_id: UUID,
    data: UpdateRoleRequest,
    current_user: User = Depends(require_permission("users.roles.manage")),
    db=Depends(get_db),
):
    return await update_role(
        db, role_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/{role_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_role_endpoint(
    role_id: UUID,
    current_user: User = Depends(require_permission("users.roles.manage")),
    db=Depends(get_db),
):
    await delete_role(db, role_id, current_user.business_id, current_user.id)
    return MessageResponse(message="Role deleted")


@router.put(
    "/{role_id}/permissions",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
)
async def replace_role_permissions_endpoint(
    role_id: UUID,
    data: AssignPermissionsRequest,
    current_user: User = Depends(require_permission("users.roles.manage")),
    db=Depends(get_db),
):
    return await replace_role_permissions(
        db, role_id, current_user.business_id, data, current_user.id
    )
