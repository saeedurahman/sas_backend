from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.enums import PurchaseOrderStatusEnum
from app.models.user import User
from app.schemas.inventory import (
    CreatePurchaseOrderRequest,
    CreatePurchaseReceiptRequest,
    PurchaseOrderResponse,
    PurchaseReceiptResponse,
)
from app.services.purchase_service import (
    create_purchase_order,
    create_purchase_receipt,
    get_purchase_order_by_id,
    get_purchase_orders,
    get_purchase_receipt_by_id,
    update_purchase_order_status,
)

router = APIRouter(prefix="/purchases", tags=["Purchases"])


class UpdatePOStatusRequest(BaseModel):
    status: PurchaseOrderStatusEnum


@router.get(
    "/orders",
    response_model=list[PurchaseOrderResponse],
    status_code=status.HTTP_200_OK,
)
async def list_purchase_orders(
    branch_id: UUID | None = Query(default=None),
    supplier_id: UUID | None = Query(default=None),
    po_status: str | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    orders, _ = await get_purchase_orders(
        db,
        current_user.business_id,
        branch_id,
        supplier_id,
        po_status,
        skip,
        limit,
    )
    return orders


@router.post(
    "/orders",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_order_endpoint(
    data: CreatePurchaseOrderRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_purchase_order(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/orders/{po_id}",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def get_purchase_order(
    po_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_purchase_order_by_id(db, po_id, current_user.business_id)


@router.put(
    "/orders/{po_id}/status",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_200_OK,
)
async def update_po_status(
    po_id: UUID,
    data: UpdatePOStatusRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await update_purchase_order_status(
        db, po_id, current_user.business_id, data.status, current_user.id
    )


@router.post(
    "/receipts",
    response_model=PurchaseReceiptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_receipt_endpoint(
    data: CreatePurchaseReceiptRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_purchase_receipt(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/receipts/{receipt_id}",
    response_model=PurchaseReceiptResponse,
    status_code=status.HTTP_200_OK,
)
async def get_purchase_receipt(
    receipt_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_purchase_receipt_by_id(
        db, receipt_id, current_user.business_id
    )
