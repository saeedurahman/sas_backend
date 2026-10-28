from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.product import BrandResponse, CreateBrandRequest, UpdateBrandRequest
from app.services.brand_service import (
    create_brand,
    delete_brand,
    get_brand_by_id,
    get_brands,
    update_brand,
)

router = APIRouter(prefix="/brands", tags=["Brands"])


@router.get("", response_model=list[BrandResponse], status_code=status.HTTP_200_OK)
async def list_brands(
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    return await get_brands(db, current_user.business_id)


@router.post("", response_model=BrandResponse, status_code=status.HTTP_201_CREATED)
async def create_brand_endpoint(
    data: CreateBrandRequest,
    current_user: User = Depends(require_permission("products.manage_brands")),
    db=Depends(get_db),
):
    return await create_brand(db, current_user.business_id, data, current_user.id)


@router.get(
    "/{brand_id}",
    response_model=BrandResponse,
    status_code=status.HTTP_200_OK,
)
async def get_brand(
    brand_id: UUID,
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    return await get_brand_by_id(db, brand_id, current_user.business_id)


@router.put(
    "/{brand_id}",
    response_model=BrandResponse,
    status_code=status.HTTP_200_OK,
)
async def update_brand_endpoint(
    brand_id: UUID,
    data: UpdateBrandRequest,
    current_user: User = Depends(require_permission("products.manage_brands")),
    db=Depends(get_db),
):
    return await update_brand(
        db, brand_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/{brand_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_brand_endpoint(
    brand_id: UUID,
    current_user: User = Depends(require_permission("products.manage_brands")),
    db=Depends(get_db),
):
    await delete_brand(db, brand_id, current_user.business_id, current_user.id)
    return MessageResponse(message="Brand deleted")
