from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.branch_scope import assert_branch_access
from app.dependencies import (
    allowed_branch_ids,
    require_manager,
    require_owner,
    require_permission,
)
from app.models.user import User
from app.schemas.business import BranchResponse, CreateBranchRequest, UpdateBranchRequest
from app.services.branch_service import (
    create_new_branch,
    get_branch_by_id,
    get_branches_for_business,
    update_branch,
)

router = APIRouter(prefix="/branches", tags=["branches"])


@router.get("", response_model=list[BranchResponse], status_code=status.HTTP_200_OK)
async def list_branches(
    current_user: User = Depends(require_permission("branches.view")),
    db=Depends(get_db),
):
    return await get_branches_for_business(
        db, current_user.business_id, allowed_branch_ids(current_user)
    )


@router.post("", response_model=BranchResponse, status_code=status.HTTP_201_CREATED)
async def create_branch(
    data: CreateBranchRequest,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await create_new_branch(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.get(
    "/{branch_id}",
    response_model=BranchResponse,
    status_code=status.HTTP_200_OK,
)
async def get_branch(
    branch_id: UUID,
    current_user: User = Depends(require_permission("branches.view")),
    db=Depends(get_db),
):
    assert_branch_access(current_user, branch_id)
    return await get_branch_by_id(db, branch_id, current_user.business_id)


@router.put(
    "/{branch_id}",
    response_model=BranchResponse,
    status_code=status.HTTP_200_OK,
)
async def update_branch_endpoint(
    branch_id: UUID,
    data: UpdateBranchRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    assert_branch_access(current_user, branch_id)
    return await update_branch(
        db, branch_id, current_user.business_id, data, current_user.id
    )
