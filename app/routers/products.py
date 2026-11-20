from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.config import settings
from app.core.idempotency import run_idempotent
from app.core.rate_limit import enforce_rate_limit
from app.database import get_db
from app.branch_scope import (
    assert_branch_access,
    effective_branch,
    guard_body_branches,
)
from app.dependencies import _user_permission_keys, allowed_branch_ids, require_permission
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.quick_add import ProductLookupResponse, QuickAddRequest, QuickAddResponse
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
from app.services.product_import_service import (
    ProductImportRequest,
    import_products,
    resolve_import_branch,
)
from app.services.quick_add_service import lookup_barcode, run_quick_add
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


@router.post("/import")
async def import_products_endpoint(
    data: ProductImportRequest,
    current_user: User = Depends(require_permission("products.create")),
    db=Depends(get_db),
):
    """CSV/rows product import. ``dry_run`` (default) validates; ``dry_run: false`` applies.

    Matched rows (by sku, then barcode) are updated, so the same file can be re-run safely.
    Applying needs ``products.update`` too, and opening stock needs ``inventory.adjust``.
    """
    keys = _user_permission_keys(current_user)
    if not data.dry_run and "products.update" not in keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: products.update"
        )
    assert_branch_access(current_user, data.branch_id)
    branch_id = await resolve_import_branch(db, current_user.business_id, data.branch_id)
    assert_branch_access(current_user, branch_id)
    return await import_products(
        db,
        current_user.business_id,
        data,
        current_user.id,
        branch_id,
        can_adjust_stock="inventory.adjust" in keys,
    )


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
    needs_attention: bool | None = Query(
        default=None,
        description="true = only products missing a category, an image or a sale price; false = the complete ones",
    ),
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
        needs_attention,
    )
    return PaginatedProductResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[ProductListResponse.model_validate(p) for p in products],
    )


@router.get(
    "/lookup",
    response_model=ProductLookupResponse,
    status_code=status.HTTP_200_OK,
)
async def lookup_product_by_barcode(
    barcode: str = Query(min_length=1, max_length=100),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_permission("products.view")),
    db=Depends(get_db),
):
    """Scan lookup: normalizes the barcode and returns this business's product (or ``found: false``).

    ``stock_on_hand`` is the given branch's; without ``branch_id`` it sums the branches you can access.
    """
    if branch_id is not None:
        assert_branch_access(current_user, branch_id)
        branch_ids: list[UUID] | None = [branch_id]
    else:
        allowed = allowed_branch_ids(current_user)
        branch_ids = None if allowed is None else sorted(allowed, key=str)
    return await lookup_barcode(db, current_user.business_id, barcode, branch_ids)


@router.post(
    "/quick-add",
    response_model=QuickAddResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(guard_body_branches)],
)
async def quick_add_endpoint(
    request: Request,
    data: QuickAddRequest,
    current_user: User = Depends(require_permission("products.create")),
    db=Depends(get_db),
):
    """Add many products (and their opening / purchased stock) from a scan session.

    Each line succeeds or fails on its own (``lines[].status``); the response is always 200 once the
    request itself is valid. Honours ``Idempotency-Key``.
    """
    keys = _user_permission_keys(current_user)
    purchase = data.mode == "purchase" and data.supplier_id is not None
    needed = (
        ("inventory.purchase_orders.create", "inventory.purchase_orders.receive")
        if purchase
        else ("inventory.adjust",)
    )
    for key in needed:
        if key not in keys:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {key}"
            )
    enforce_rate_limit("quick-add", str(current_user.id), settings.rate_limit_quick_add_user)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.quick_add_max_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body is larger than {settings.quick_add_max_body_bytes} bytes",
        )
    if len(data.lines) > settings.quick_add_max_lines:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"At most {settings.quick_add_max_lines} lines per request",
        )
    branch_id = effective_branch(current_user, data.branch_id)
    if branch_id is None:
        branch_id = await resolve_import_branch(db, current_user.business_id, None)
    if branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_id is required: this business has more than one branch",
        )
    can_update_prices = "products.update" in keys
    return await run_idempotent(
        request,
        business_id=current_user.business_id,
        user_id=current_user.id,
        body=data,
        response_model=QuickAddResponse,
        success_status=200,
        handler=lambda: run_quick_add(
            db,
            current_user.business_id,
            current_user.id,
            data,
            branch_id=branch_id,
            can_update_prices=can_update_prices,
        ),
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
