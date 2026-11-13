from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.product import (
    CreatePriceListRequest,
    PriceListItemResponse,
    PriceListResponse,
    SetPriceRequest,
)
from app.schemas.pricing import (
    AdjustPricesRequest,
    AdjustPricesResponse,
    BulkPriceRequest,
    BulkPriceResponse,
    CopyPriceListRequest,
    PaginatedPriceListItems,
    UpdatePriceListRequest,
)
from app.services import price_admin_service as admin
from app.services.import_common import ImportRequest
from app.services.price_service import (
    create_price_list,
    get_price_lists,
    get_product_price,
    set_price,
)

router = APIRouter(prefix="/prices", tags=["Prices"])


class ProductPriceResponse(BaseModel):
    unit_price: Decimal | None


@router.get(
    "/lists",
    response_model=list[PriceListResponse],
    status_code=status.HTTP_200_OK,
)
async def list_price_lists(
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    return await get_price_lists(db, current_user.business_id)


@router.post(
    "/lists",
    response_model=PriceListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_price_list_endpoint(
    data: CreatePriceListRequest,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    return await create_price_list(
        db, current_user.business_id, data, current_user.id
    )


@router.get("/lists/{list_id}", response_model=PriceListResponse)
async def get_price_list_endpoint(
    list_id: UUID,
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    return await admin.get_price_list(db, current_user.business_id, list_id)


@router.put("/lists/{list_id}", response_model=PriceListResponse)
async def update_price_list_endpoint(
    list_id: UUID,
    data: UpdatePriceListRequest,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    return await admin.update_price_list(db, current_user.business_id, list_id, data, current_user.id)


@router.delete("/lists/{list_id}")
async def delete_price_list_endpoint(
    list_id: UUID,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    """Soft delete. The default list cannot be deleted; customers on it fall back to default."""
    return await admin.delete_price_list(db, current_user.business_id, list_id, current_user.id)


@router.post(
    "/lists/{list_id}/copy",
    response_model=PriceListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def copy_price_list_endpoint(
    list_id: UUID,
    data: CopyPriceListRequest,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    return await admin.copy_price_list(db, current_user.business_id, list_id, data, current_user.id)


@router.get("/lists/{list_id}/items", response_model=PaginatedPriceListItems)
async def list_price_items(
    list_id: UUID,
    search: str | None = Query(default=None, max_length=100),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    items, total = await admin.list_items(
        db, current_user.business_id, list_id, search, skip, limit
    )
    return PaginatedPriceListItems(total=total, skip=skip, limit=limit, items=items)


@router.post("/lists/{list_id}/items/bulk", response_model=BulkPriceResponse)
async def bulk_upsert_price_items(
    list_id: UUID,
    data: BulkPriceRequest,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    return await admin.bulk_upsert_items(db, current_user.business_id, list_id, data, current_user.id)


@router.delete("/lists/{list_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_price_item(
    list_id: UUID,
    item_id: UUID,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    await admin.delete_item(db, current_user.business_id, list_id, item_id, current_user.id)


@router.post("/lists/{list_id}/adjust", response_model=AdjustPricesResponse)
async def adjust_price_list(
    list_id: UUID,
    data: AdjustPricesRequest,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    """Raise/lower every price (optionally one category) by ``percent``; ``dry_run`` previews."""
    return await admin.adjust_prices(db, current_user.business_id, list_id, data, current_user.id)


@router.post("/lists/{list_id}/import")
async def import_price_list_csv(
    list_id: UUID,
    data: ImportRequest,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    """CSV price import: send ``dry_run: true`` for the validation report, then ``false``."""
    return await admin.import_prices(db, current_user.business_id, list_id, data, current_user.id)


@router.post(
    "/lists/{list_id}/items",
    response_model=PriceListItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def set_price_endpoint(
    list_id: UUID,
    data: SetPriceRequest,
    current_user: User = Depends(require_permission("products.manage_prices")),
    db=Depends(get_db),
):
    return await set_price(
        db, list_id, current_user.business_id, data, current_user.id
    )


@router.get(
    "/product/{product_id}",
    response_model=ProductPriceResponse,
    status_code=status.HTTP_200_OK,
)
async def get_product_price_endpoint(
    product_id: UUID,
    variation_id: UUID | None = Query(default=None),
    price_list_id: UUID | None = Query(default=None),
    customer_id: UUID | None = Query(
        default=None, description="Use this customer's assigned price list"
    ),
    qty: Decimal = Query(default=Decimal("1"), gt=0),
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    unit_price = await get_product_price(
        db,
        current_user.business_id,
        product_id,
        variation_id,
        price_list_id,
        customer_id=customer_id,
        qty=qty,
    )
    await db.commit()  # persists a lazily created default price list, if any
    return ProductPriceResponse(unit_price=unit_price)
