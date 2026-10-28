from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.product import (
    BarcodeResponse,
    CreateBarcodeRequest,
    CreateProductRequest,
    CreateVariationRequest,
    PaginatedProductResponse,
    ProductListResponse,
    ProductResponse,
    UpdateProductRequest,
    UpdateVariationRequest,
    VariationResponse,
)
from app.services.product_service import (
    add_barcode,
    add_variation,
    create_product,
    delete_product,
    get_product_by_id,
    get_products,
    search_by_barcode,
    update_product,
    update_variation,
)

router = APIRouter(prefix="/products", tags=["Products"])


@router.get(
    "",
    response_model=PaginatedProductResponse,
    status_code=status.HTTP_200_OK,
)
async def list_products(
    category_id: UUID | None = Query(default=None),
    brand_id: UUID | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    search: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    products, total = await get_products(
        db,
        current_user.business_id,
        category_id,
        brand_id,
        is_active,
        search,
        skip,
        limit,
    )
    return PaginatedProductResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[ProductListResponse.model_validate(p) for p in products],
    )


@router.get(
    "/barcode/{barcode}",
    response_model=ProductResponse,
    status_code=status.HTTP_200_OK,
)
async def get_product_by_barcode(
    barcode: str,
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    return await search_by_barcode(db, current_user.business_id, barcode)


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product_endpoint(
    data: CreateProductRequest,
    current_user: User = Depends(require_permission("products.create")),
    db=Depends(get_db),
):
    return await create_product(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{product_id}",
    response_model=ProductResponse,
    status_code=status.HTTP_200_OK,
)
async def get_product(
    product_id: UUID,
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    return await get_product_by_id(db, product_id, current_user.business_id)


@router.put(
    "/{product_id}",
    response_model=ProductResponse,
    status_code=status.HTTP_200_OK,
)
async def update_product_endpoint(
    product_id: UUID,
    data: UpdateProductRequest,
    current_user: User = Depends(require_permission("products.update")),
    db=Depends(get_db),
):
    return await update_product(
        db, product_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/{product_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_product_endpoint(
    product_id: UUID,
    current_user: User = Depends(require_permission("products.delete")),
    db=Depends(get_db),
):
    await delete_product(
        db, product_id, current_user.business_id, current_user.id
    )
    return MessageResponse(message="Product deleted")


@router.post(
    "/{product_id}/variations",
    response_model=VariationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_variation_endpoint(
    product_id: UUID,
    data: CreateVariationRequest,
    current_user: User = Depends(require_permission("products.update")),
    db=Depends(get_db),
):
    return await add_variation(
        db, product_id, current_user.business_id, data, current_user.id
    )


@router.put(
    "/{product_id}/variations/{variation_id}",
    response_model=VariationResponse,
    status_code=status.HTTP_200_OK,
)
async def update_variation_endpoint(
    product_id: UUID,
    variation_id: UUID,
    data: UpdateVariationRequest,
    current_user: User = Depends(require_permission("products.update")),
    db=Depends(get_db),
):
    return await update_variation(
        db,
        variation_id,
        product_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.post(
    "/{product_id}/barcodes",
    response_model=BarcodeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_barcode_endpoint(
    product_id: UUID,
    data: CreateBarcodeRequest,
    current_user: User = Depends(require_permission("products.manage_barcodes")),
    db=Depends(get_db),
):
    return await add_barcode(
        db, product_id, current_user.business_id, data, current_user.id
    )
