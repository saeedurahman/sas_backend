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
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    unit_price = await get_product_price(
        db,
        current_user.business_id,
        product_id,
        variation_id,
        price_list_id,
    )
    return ProductPriceResponse(unit_price=unit_price)
