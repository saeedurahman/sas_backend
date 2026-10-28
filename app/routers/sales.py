from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import (
    _user_permission_keys,
    require_feature_flag,
    require_feature_flags,
    require_permission,
)
from app.models.business import BusinessConfig
from app.models.user import User
from app.schemas.sales import (
    AddSaleLinesRequest,
    CompleteTabRequest,
    CreateSaleRequest,
    FireToKitchenRequest,
    OpenTabRequest,
    PaginatedSaleResponse,
    RequestBillRequest,
    SaleListResponse,
    SalePricePreviewResponse,
    SaleResponse,
)
from app.services.restaurant_tab_service import (
    add_lines_to_tab,
    complete_tab,
    fire_tab_to_kitchen,
    open_tab,
    request_tab_bill,
)
from app.services.sale_service import (
    cancel_sale,
    compute_sale_pricing,
    create_sale,
    get_sale_by_id,
    get_sales,
    void_sale,
)

router = APIRouter(prefix="/sales", tags=["Sales"])


@router.get(
    "",
    response_model=PaginatedSaleResponse,
    status_code=status.HTTP_200_OK,
)
async def list_sales(
    branch_id: UUID | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    sale_status: str | None = Query(default=None, alias="status"),
    sale_type: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    search: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    items, total = await get_sales(
        db,
        current_user.business_id,
        branch_id,
        customer_id,
        sale_status,
        sale_type,
        date_from,
        date_to,
        search,
        skip,
        limit,
    )
    return PaginatedSaleResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[SaleListResponse.model_validate(item) for item in items],
    )


@router.post("", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
async def create_sale_endpoint(
    data: CreateSaleRequest,
    current_user: User = Depends(require_permission("sales.create")),
    db=Depends(get_db),
):
    return await create_sale(
        db,
        current_user.business_id,
        data,
        current_user.id,
        _user_permission_keys(current_user),
    )


@router.post(
    "/price-preview",
    response_model=SalePricePreviewResponse,
    status_code=status.HTTP_200_OK,
)
async def preview_sale_price(
    data: CreateSaleRequest,
    current_user: User = Depends(require_permission("sales.create")),
    db=Depends(get_db),
):
    return await compute_sale_pricing(
        db,
        current_user.business_id,
        data,
        _user_permission_keys(current_user),
    )


@router.post(
    "/open-tab",
    response_model=SaleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_tab_endpoint(
    data: OpenTabRequest,
    current_user: User = Depends(require_permission("sales.create")),
    _config: BusinessConfig = Depends(
        require_feature_flags("enable_restaurant", "enable_table_management")
    ),
    db=Depends(get_db),
):
    return await open_tab(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.post(
    "/{sale_id}/lines",
    response_model=SaleResponse,
    status_code=status.HTTP_200_OK,
)
async def add_tab_lines_endpoint(
    sale_id: UUID,
    data: AddSaleLinesRequest,
    current_user: User = Depends(require_permission("sales.create")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await add_lines_to_tab(
        db,
        current_user.business_id,
        sale_id,
        data,
        current_user.id,
        _user_permission_keys(current_user),
    )


@router.post(
    "/{sale_id}/fire",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
async def fire_tab_endpoint(
    sale_id: UUID,
    data: FireToKitchenRequest | None = None,
    current_user: User = Depends(require_permission("restaurant.kot.fire")),
    _config: BusinessConfig = Depends(
        require_feature_flags("enable_restaurant", "enable_kot")
    ),
    db=Depends(get_db),
):
    payload = data or FireToKitchenRequest()
    kot_order = await fire_tab_to_kitchen(
        db,
        current_user.business_id,
        sale_id,
        sale_line_ids=payload.sale_line_ids,
        notes=payload.notes,
        created_by=current_user.id,
    )
    return {
        "id": kot_order.id,
        "kot_number": kot_order.kot_number,
        "sale_id": kot_order.sale_id,
        "table_id": kot_order.table_id,
        "status": kot_order.status,
    }


@router.post(
    "/{sale_id}/request-bill",
    response_model=SaleResponse,
    status_code=status.HTTP_200_OK,
)
async def request_tab_bill_endpoint(
    sale_id: UUID,
    _data: RequestBillRequest | None = None,
    current_user: User = Depends(require_permission("sales.create")),
    _config: BusinessConfig = Depends(
        require_feature_flags("enable_restaurant", "enable_table_management")
    ),
    db=Depends(get_db),
):
    return await request_tab_bill(
        db,
        current_user.business_id,
        sale_id,
        current_user.id,
    )


@router.post(
    "/{sale_id}/complete",
    response_model=SaleResponse,
    status_code=status.HTTP_200_OK,
)
async def complete_tab_endpoint(
    sale_id: UUID,
    data: CompleteTabRequest,
    current_user: User = Depends(require_permission("sales.create")),
    _config: BusinessConfig = Depends(
        require_feature_flags("enable_restaurant", "enable_table_management")
    ),
    db=Depends(get_db),
):
    return await complete_tab(
        db,
        current_user.business_id,
        sale_id,
        data,
        current_user.id,
        _user_permission_keys(current_user),
    )


@router.get(
    "/{sale_id}",
    response_model=SaleResponse,
    status_code=status.HTTP_200_OK,
)
async def get_sale(
    sale_id: UUID,
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    return await get_sale_by_id(db, sale_id, current_user.business_id)


@router.put(
    "/{sale_id}/cancel",
    response_model=SaleResponse,
    status_code=status.HTTP_200_OK,
)
async def cancel_sale_endpoint(
    sale_id: UUID,
    current_user: User = Depends(require_permission("sales.cancel")),
    db=Depends(get_db),
):
    return await cancel_sale(
        db, sale_id, current_user.business_id, current_user.id
    )


@router.post(
    "/{sale_id}/void",
    response_model=SaleResponse,
    status_code=status.HTTP_200_OK,
)
async def void_sale_endpoint(
    sale_id: UUID,
    current_user: User = Depends(require_permission("sales.cancel")),
    db=Depends(get_db),
):
    return await void_sale(
        db, sale_id, current_user.business_id, current_user.id
    )
