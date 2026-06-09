from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.product import (
    CategoryResponse,
    CreateCategoryRequest,
    UpdateCategoryRequest,
)
from app.services.category_service import (
    create_category,
    delete_category,
    get_categories,
    get_category_by_id,
    update_category,
)

router = APIRouter(prefix="/categories", tags=["Categories"])


@router.get("", response_model=list[CategoryResponse], status_code=status.HTTP_200_OK)
async def list_categories(
    parent_id: UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_categories(db, current_user.business_id, parent_id)


@router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category_endpoint(
    data: CreateCategoryRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_category(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{category_id}",
    response_model=CategoryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_category(
    category_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_category_by_id(db, category_id, current_user.business_id)


@router.put(
    "/{category_id}",
    response_model=CategoryResponse,
    status_code=status.HTTP_200_OK,
)
async def update_category_endpoint(
    category_id: UUID,
    data: UpdateCategoryRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await update_category(
        db, category_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/{category_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_category_endpoint(
    category_id: UUID,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    await delete_category(
        db, category_id, current_user.business_id, current_user.id
    )
    return MessageResponse(message="Category deleted")
