from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import (
    _user_permission_keys,
    require_feature_flag,
    require_permission,
)
from app.models.business import BusinessConfig
from app.models.enums import TableStatusEnum
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.restaurant import (
    CreateDiningTableRequest,
    CreateFloorPlanRequest,
    DiningTableResponse,
    FloorLayoutResponse,
    FloorPlanResponse,
    UpdateDiningTableRequest,
    UpdateFloorPlanRequest,
    UpdateTableStatusRequest,
)
from app.services.restaurant_table_service import (
    create_dining_table,
    create_floor_plan,
    delete_dining_table,
    delete_floor_plan,
    get_dining_table_response,
    get_floor_layout,
    get_floor_plan_by_id,
    get_floor_plans,
    list_dining_table_responses,
    update_dining_table,
    update_dining_table_status,
    update_floor_plan,
)

router = APIRouter(prefix="/restaurant", tags=["Restaurant"])


@router.get(
    "/floor-plans",
    response_model=list[FloorPlanResponse],
    status_code=status.HTTP_200_OK,
)
async def list_floor_plans(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_permission("restaurant.floor_plans.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await get_floor_plans(
        db,
        current_user.business_id,
        branch_id=branch_id,
    )


@router.post(
    "/floor-plans",
    response_model=FloorPlanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_floor_plan_endpoint(
    data: CreateFloorPlanRequest,
    current_user: User = Depends(require_permission("restaurant.floor_plans.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await create_floor_plan(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.get(
    "/floor-plans/{floor_plan_id}",
    response_model=FloorPlanResponse,
    status_code=status.HTTP_200_OK,
)
async def get_floor_plan(
    floor_plan_id: UUID,
    current_user: User = Depends(require_permission("restaurant.floor_plans.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await get_floor_plan_by_id(
        db,
        floor_plan_id,
        current_user.business_id,
    )


@router.put(
    "/floor-plans/{floor_plan_id}",
    response_model=FloorPlanResponse,
    status_code=status.HTTP_200_OK,
)
async def update_floor_plan_endpoint(
    floor_plan_id: UUID,
    data: UpdateFloorPlanRequest,
    current_user: User = Depends(require_permission("restaurant.floor_plans.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await update_floor_plan(
        db,
        floor_plan_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.delete(
    "/floor-plans/{floor_plan_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_floor_plan_endpoint(
    floor_plan_id: UUID,
    current_user: User = Depends(require_permission("restaurant.floor_plans.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    await delete_floor_plan(
        db,
        floor_plan_id,
        current_user.business_id,
        current_user.id,
    )
    return MessageResponse(message="Floor plan deleted")


@router.get(
    "/tables",
    response_model=list[DiningTableResponse],
    status_code=status.HTTP_200_OK,
)
async def list_dining_tables(
    branch_id: UUID | None = Query(default=None),
    floor_plan_id: UUID | None = Query(default=None),
    table_status: TableStatusEnum | None = Query(default=None, alias="status"),
    current_user: User = Depends(require_permission("restaurant.tables.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await list_dining_table_responses(
        db,
        current_user.business_id,
        branch_id=branch_id,
        floor_plan_id=floor_plan_id,
        table_status=table_status,
    )


@router.post(
    "/tables",
    response_model=DiningTableResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dining_table_endpoint(
    data: CreateDiningTableRequest,
    current_user: User = Depends(require_permission("restaurant.tables.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await create_dining_table(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.get(
    "/tables/{table_id}",
    response_model=DiningTableResponse,
    status_code=status.HTTP_200_OK,
)
async def get_dining_table(
    table_id: UUID,
    current_user: User = Depends(require_permission("restaurant.tables.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await get_dining_table_response(
        db,
        table_id,
        current_user.business_id,
    )


@router.put(
    "/tables/{table_id}",
    response_model=DiningTableResponse,
    status_code=status.HTTP_200_OK,
)
async def update_dining_table_endpoint(
    table_id: UUID,
    data: UpdateDiningTableRequest,
    current_user: User = Depends(require_permission("restaurant.tables.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await update_dining_table(
        db,
        table_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.patch(
    "/tables/{table_id}/status",
    response_model=DiningTableResponse,
    status_code=status.HTTP_200_OK,
)
async def update_dining_table_status_endpoint(
    table_id: UUID,
    data: UpdateTableStatusRequest,
    current_user: User = Depends(require_permission("restaurant.tables.update_status")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    force = data.force
    if force and "restaurant.tables.manage" not in _user_permission_keys(current_user):
        force = False

    return await update_dining_table_status(
        db,
        table_id,
        current_user.business_id,
        data.status,
        current_user.id,
        force=force,
    )


@router.delete(
    "/tables/{table_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_dining_table_endpoint(
    table_id: UUID,
    current_user: User = Depends(require_permission("restaurant.tables.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    await delete_dining_table(
        db,
        table_id,
        current_user.business_id,
        current_user.id,
    )
    return MessageResponse(message="Table deleted")


@router.get(
    "/floor-layout",
    response_model=FloorLayoutResponse,
    status_code=status.HTTP_200_OK,
)
async def get_floor_layout_endpoint(
    branch_id: UUID = Query(...),
    current_user: User = Depends(require_permission("restaurant.tables.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_table_management")),
    db=Depends(get_db),
):
    return await get_floor_layout(
        db,
        current_user.business_id,
        branch_id,
    )
