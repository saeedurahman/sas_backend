from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel

from app.core.idempotency import run_idempotent
from app.branch_scope import assert_branch_access, guard_body_branches, guard_record, scoped_branch_query
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
from app.services.receipt_link_service import (
    find_sale_by_number,
    issue_for_sale_id,
    issue_link,
    revoke_links_for_sale,
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

router = APIRouter(prefix="/sales", tags=["Sales"], dependencies=[Depends(guard_body_branches)])


class ReceiptLinkResponse(BaseModel):
    url: str
    expires_at: datetime


class ReceiptLinkLookupRequest(BaseModel):
    """Find a sale by its official number and/or the receipt number printed offline."""

    sale_number: str | None = None
    client_receipt_no: str | None = None


@router.post(
    "/receipt-link/lookup",
    response_model=ReceiptLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_receipt_link_by_number(
    request: Request,
    data: ReceiptLinkLookupRequest,
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    sale = await find_sale_by_number(
        db,
        current_user.business_id,
        sale_number=data.sale_number,
        client_receipt_no=data.client_receipt_no,
    )
    assert_branch_access(current_user, sale.branch_id)
    issued = await issue_link(
        db, current_user.business_id, sale, current_user.id, str(request.base_url)
    )
    return ReceiptLinkResponse(url=issued.url, expires_at=issued.expires_at)


@router.post(
    "/{sale_id}/receipt-link",
    response_model=ReceiptLinkResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(guard_record("sales", "sale_id"))],
)
async def create_receipt_link(
    request: Request,
    sale_id: UUID,
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    issued = await issue_for_sale_id(
        db, current_user.business_id, sale_id, current_user.id, str(request.base_url)
    )
    return ReceiptLinkResponse(url=issued.url, expires_at=issued.expires_at)


@router.delete(
    "/{sale_id}/receipt-link",
    dependencies=[Depends(guard_record("sales", "sale_id"))],
)
async def revoke_receipt_links(
    sale_id: UUID,
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    """Revoke every active public link of this sale."""
    revoked = await revoke_links_for_sale(db, current_user.business_id, sale_id, current_user.id)
    return {"revoked": revoked}


@router.get(
    "",
    response_model=PaginatedSaleResponse,
    status_code=status.HTTP_200_OK,
)
async def list_sales(
    branch_id: UUID | None = Depends(scoped_branch_query),
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
    request: Request,
    data: CreateSaleRequest,
    current_user: User = Depends(require_permission("sales.create")),
    db=Depends(get_db),
):
    return await run_idempotent(
        request,
        business_id=current_user.business_id,
        user_id=current_user.id,
        body=data,
        response_model=SaleResponse,
        success_status=201,
        handler=lambda: create_sale(
        db,
        current_user.business_id,
        data,
        current_user.id,
        _user_permission_keys(current_user),
    ),
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
    dependencies=[Depends(guard_record("sales", "sale_id"))],
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
    dependencies=[Depends(guard_record("sales", "sale_id"))],
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
    dependencies=[Depends(guard_record("sales", "sale_id"))],
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
    dependencies=[Depends(guard_record("sales", "sale_id"))],
)
async def complete_tab_endpoint(
    request: Request,
    sale_id: UUID,
    data: CompleteTabRequest,
    current_user: User = Depends(require_permission("sales.create")),
    _config: BusinessConfig = Depends(
        require_feature_flags("enable_restaurant", "enable_table_management")
    ),
    db=Depends(get_db),
):
    return await run_idempotent(
        request,
        business_id=current_user.business_id,
        user_id=current_user.id,
        body=data,
        response_model=SaleResponse,
        success_status=200,
        handler=lambda: complete_tab(
        db,
        current_user.business_id,
        sale_id,
        data,
        current_user.id,
        _user_permission_keys(current_user),
    ),
    )


@router.get(
    "/{sale_id}",
    response_model=SaleResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(guard_record("sales", "sale_id"))],
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
    dependencies=[Depends(guard_record("sales", "sale_id"))],
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
    dependencies=[Depends(guard_record("sales", "sale_id"))],
)
async def void_sale_endpoint(
    sale_id: UUID,
    current_user: User = Depends(require_permission("sales.void")),
    db=Depends(get_db),
):
    return await void_sale(
        db, sale_id, current_user.business_id, current_user.id
    )
