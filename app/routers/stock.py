from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.inventory import (
    PaginatedStockMovementResponse,
    StockBalanceResponse,
    StockMovementResponse,
)
from app.services.stock_service import (
    get_stock_balance_detail,
    get_stock_balances_for_branch,
    get_stock_movements,
)

router = APIRouter(prefix="/stock", tags=["Stock"])


@router.get(
    "/balance",
    response_model=StockBalanceResponse,
    status_code=status.HTTP_200_OK,
)
async def get_balance(
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_permission("inventory.view")),
    db=Depends(get_db),
):
    return await get_stock_balance_detail(
        db,
        current_user.business_id,
        branch_id,
        product_id,
        variation_id,
    )


@router.get(
    "/balances",
    response_model=list[StockBalanceResponse],
    status_code=status.HTTP_200_OK,
)
async def list_balances(
    branch_id: UUID,
    product_ids: list[UUID] | None = Query(default=None),
    current_user: User = Depends(require_permission("inventory.view")),
    db=Depends(get_db),
):
    return await get_stock_balances_for_branch(
        db, current_user.business_id, branch_id, product_ids
    )


@router.get(
    "/movements",
    response_model=PaginatedStockMovementResponse,
    status_code=status.HTTP_200_OK,
)
async def list_movements(
    branch_id: UUID | None = Query(default=None),
    product_id: UUID | None = Query(default=None),
    movement_type: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("inventory.view")),
    db=Depends(get_db),
):
    movements, total = await get_stock_movements(
        db,
        current_user.business_id,
        branch_id,
        product_id,
        movement_type,
        skip,
        limit,
    )
    return PaginatedStockMovementResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[StockMovementResponse.model_validate(m) for m in movements],
    )
