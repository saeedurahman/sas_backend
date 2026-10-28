from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_feature_flag, require_permission
from app.models.business import BusinessConfig
from app.models.user import User
from app.schemas.restaurant import (
    KotOrderResponse,
    UpdateKotOrderLineStatusRequest,
    UpdateKotOrderStatusRequest,
)
from app.services.restaurant_kot_service import (
    get_active_kot_orders,
    get_kot_order_by_id,
    get_kot_orders_by_table,
    update_kot_order_line_status,
    update_kot_order_status,
)

router = APIRouter(prefix="/restaurant/kot", tags=["Restaurant KOT"])


@router.get(
    "/active",
    response_model=list[KotOrderResponse],
    status_code=status.HTTP_200_OK,
)
async def list_active_kot_orders(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_permission("restaurant.kot.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_kot")),
    db=Depends(get_db),
):
    return await get_active_kot_orders(
        db,
        current_user.business_id,
        branch_id=branch_id,
    )


@router.get(
    "/by-table/{table_id}",
    response_model=list[KotOrderResponse],
    status_code=status.HTTP_200_OK,
)
async def list_kot_orders_by_table(
    table_id: UUID,
    current_user: User = Depends(require_permission("restaurant.kot.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_kot")),
    db=Depends(get_db),
):
    return await get_kot_orders_by_table(
        db,
        table_id,
        current_user.business_id,
    )


@router.get(
    "/{kot_id}",
    response_model=KotOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def get_kot_order(
    kot_id: UUID,
    current_user: User = Depends(require_permission("restaurant.kot.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_kot")),
    db=Depends(get_db),
):
    return await get_kot_order_by_id(db, kot_id, current_user.business_id)


@router.patch(
    "/{kot_id}/status",
    response_model=KotOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def patch_kot_order_status(
    kot_id: UUID,
    data: UpdateKotOrderStatusRequest,
    current_user: User = Depends(require_permission("restaurant.kot.update_status")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_kot")),
    db=Depends(get_db),
):
    return await update_kot_order_status(
        db,
        kot_id,
        current_user.business_id,
        data.status,
        current_user.id,
    )


@router.patch(
    "/lines/{line_id}/status",
    response_model=KotOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def patch_kot_order_line_status(
    line_id: UUID,
    data: UpdateKotOrderLineStatusRequest,
    current_user: User = Depends(require_permission("restaurant.kot.update_status")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_kot")),
    db=Depends(get_db),
):
    return await update_kot_order_line_status(
        db,
        line_id,
        current_user.business_id,
        data.status,
        current_user.id,
    )
