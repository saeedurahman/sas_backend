from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.inventory import (
    CreateStockTransferRequest,
    StockTransferResponse,
)
from app.services.transfer_service import (
    create_stock_transfer,
    get_stock_transfer_by_id,
    get_stock_transfers,
    receive_stock_transfer,
)

router = APIRouter(prefix="/transfers", tags=["Transfers"])


@router.get(
    "",
    response_model=list[StockTransferResponse],
    status_code=status.HTTP_200_OK,
)
async def list_transfers(
    branch_id: UUID | None = Query(default=None),
    transfer_status: str | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    transfers, _ = await get_stock_transfers(
        db, current_user.business_id, branch_id, transfer_status, skip, limit
    )
    return transfers


@router.post(
    "",
    response_model=StockTransferResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_transfer(
    data: CreateStockTransferRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_stock_transfer(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{transfer_id}",
    response_model=StockTransferResponse,
    status_code=status.HTTP_200_OK,
)
async def get_transfer(
    transfer_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_stock_transfer_by_id(
        db, transfer_id, current_user.business_id
    )


@router.post(
    "/{transfer_id}/receive",
    response_model=StockTransferResponse,
    status_code=status.HTTP_200_OK,
)
async def receive_transfer(
    transfer_id: UUID,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await receive_stock_transfer(
        db, transfer_id, current_user.business_id, current_user.id
    )
