from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.inventory import (
    CreateStockAdjustmentRequest,
    StockAdjustmentResponse,
)
from app.services.adjustment_service import (
    create_stock_adjustment,
    get_stock_adjustment_by_id,
    get_stock_adjustments,
)

router = APIRouter(prefix="/adjustments", tags=["Adjustments"])


@router.get(
    "",
    response_model=list[StockAdjustmentResponse],
    status_code=status.HTTP_200_OK,
)
async def list_adjustments(
    branch_id: UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("inventory.view")),
    db=Depends(get_db),
):
    adjustments, _ = await get_stock_adjustments(
        db, current_user.business_id, branch_id, skip, limit
    )
    return adjustments


@router.post(
    "",
    response_model=StockAdjustmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_adjustment(
    data: CreateStockAdjustmentRequest,
    current_user: User = Depends(require_permission("inventory.adjust")),
    db=Depends(get_db),
):
    return await create_stock_adjustment(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{adj_id}",
    response_model=StockAdjustmentResponse,
    status_code=status.HTTP_200_OK,
)
async def get_adjustment(
    adj_id: UUID,
    current_user: User = Depends(require_permission("inventory.view")),
    db=Depends(get_db),
):
    return await get_stock_adjustment_by_id(
        db, adj_id, current_user.business_id
    )
