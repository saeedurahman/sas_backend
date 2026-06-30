from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import _user_permission_keys, require_permission
from app.models.user import User
from app.schemas.sales import (
    CreateSaleRequest,
    PaginatedSaleResponse,
    SaleListResponse,
    SalePricePreviewResponse,
    SaleResponse,
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
