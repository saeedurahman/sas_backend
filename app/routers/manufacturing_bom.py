from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_feature_flag, require_permission
from app.models.business import BusinessConfig
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.manufacturing import (
    BomHeaderResponse,
    BomPreviewRequest,
    BomPreviewResponse,
    CreateBomRequest,
    UpdateBomRequest,
)
from app.services.manufacturing_bom_service import (
    create_bom_header,
    delete_bom_header,
    get_bom_header_by_id,
    get_bom_headers,
    get_boms_by_product,
    preview_bom_requirements,
    update_bom_header,
)

router = APIRouter(prefix="/manufacturing", tags=["Manufacturing"])


@router.get(
    "/boms",
    response_model=list[BomHeaderResponse],
    status_code=status.HTTP_200_OK,
)
async def list_bom_headers(
    product_id: UUID | None = Query(default=None),
    active_only: bool = Query(default=False),
    current_user: User = Depends(require_permission("manufacturing.bom.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await get_bom_headers(
        db,
        current_user.business_id,
        product_id=product_id,
        active_only=active_only,
    )


@router.post(
    "/boms",
    response_model=BomHeaderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_bom_endpoint(
    data: CreateBomRequest,
    current_user: User = Depends(require_permission("manufacturing.bom.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await create_bom_header(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.get(
    "/boms/by-product/{product_id}",
    response_model=list[BomHeaderResponse],
    status_code=status.HTTP_200_OK,
)
async def list_boms_by_product(
    product_id: UUID,
    variation_id: UUID | None = Query(default=None),
    active_only: bool = Query(default=False),
    current_user: User = Depends(require_permission("manufacturing.bom.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await get_boms_by_product(
        db,
        product_id,
        current_user.business_id,
        variation_id=variation_id,
        active_only=active_only,
    )


@router.post(
    "/boms/preview",
    response_model=BomPreviewResponse,
    status_code=status.HTTP_200_OK,
)
async def preview_bom_endpoint(
    data: BomPreviewRequest,
    current_user: User = Depends(require_permission("manufacturing.bom.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await preview_bom_requirements(
        db,
        current_user.business_id,
        data.bom_header_id,
        data.qty_to_produce,
    )


@router.get(
    "/boms/{bom_id}",
    response_model=BomHeaderResponse,
    status_code=status.HTTP_200_OK,
)
async def get_bom_endpoint(
    bom_id: UUID,
    current_user: User = Depends(require_permission("manufacturing.bom.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await get_bom_header_by_id(db, bom_id, current_user.business_id)


@router.put(
    "/boms/{bom_id}",
    response_model=BomHeaderResponse,
    status_code=status.HTTP_200_OK,
)
async def update_bom_endpoint(
    bom_id: UUID,
    data: UpdateBomRequest,
    current_user: User = Depends(require_permission("manufacturing.bom.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await update_bom_header(
        db,
        bom_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.delete(
    "/boms/{bom_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_bom_endpoint(
    bom_id: UUID,
    current_user: User = Depends(require_permission("manufacturing.bom.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    await delete_bom_header(
        db,
        bom_id,
        current_user.business_id,
        current_user.id,
    )
    return MessageResponse(message="BOM deleted")
