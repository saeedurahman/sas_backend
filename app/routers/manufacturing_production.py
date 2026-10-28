from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_feature_flag, require_permission
from app.models.business import BusinessConfig
from app.models.user import User
from app.schemas.manufacturing import (
    CompleteProductionOrderRequest,
    CreateProductionOrderRequest,
    ProductionOrderResponse,
    UpdateProductionOrderRequest,
)
from app.services.manufacturing_production_service import (
    cancel_production_order,
    complete_production_order,
    create_production_order,
    get_production_order_by_id,
    get_production_orders,
    start_production_order,
    update_production_order,
)

router = APIRouter(prefix="/manufacturing", tags=["Manufacturing"])


@router.get(
    "/production-orders",
    response_model=list[ProductionOrderResponse],
    status_code=status.HTTP_200_OK,
)
async def list_production_orders(
    branch_id: UUID | None = Query(default=None),
    order_status: str | None = Query(default=None, alias="status"),
    bom_header_id: UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("manufacturing.production.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await get_production_orders(
        db,
        current_user.business_id,
        branch_id=branch_id,
        status_filter=order_status,
        bom_header_id=bom_header_id,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/production-orders",
    response_model=ProductionOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_production_order_endpoint(
    data: CreateProductionOrderRequest,
    current_user: User = Depends(require_permission("manufacturing.production.create")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await create_production_order(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.get(
    "/production-orders/{order_id}",
    response_model=ProductionOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def get_production_order_endpoint(
    order_id: UUID,
    current_user: User = Depends(require_permission("manufacturing.production.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await get_production_order_by_id(
        db, order_id, current_user.business_id
    )


@router.put(
    "/production-orders/{order_id}",
    response_model=ProductionOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def update_production_order_endpoint(
    order_id: UUID,
    data: UpdateProductionOrderRequest,
    current_user: User = Depends(require_permission("manufacturing.production.create")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await update_production_order(
        db,
        order_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.post(
    "/production-orders/{order_id}/start",
    response_model=ProductionOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def start_production_order_endpoint(
    order_id: UUID,
    current_user: User = Depends(require_permission("manufacturing.production.create")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await start_production_order(
        db,
        order_id,
        current_user.business_id,
        current_user.id,
    )


@router.post(
    "/production-orders/{order_id}/cancel",
    response_model=ProductionOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def cancel_production_order_endpoint(
    order_id: UUID,
    current_user: User = Depends(require_permission("manufacturing.production.cancel")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await cancel_production_order(
        db,
        order_id,
        current_user.business_id,
        current_user.id,
    )


@router.post(
    "/production-orders/{order_id}/complete",
    response_model=ProductionOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def complete_production_order_endpoint(
    order_id: UUID,
    data: CompleteProductionOrderRequest,
    current_user: User = Depends(require_permission("manufacturing.production.complete")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_manufacturing")),
    db=Depends(get_db),
):
    return await complete_production_order(
        db,
        order_id,
        current_user.business_id,
        data,
        current_user.id,
    )
