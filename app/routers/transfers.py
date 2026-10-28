from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.idempotency import run_idempotent
from app.branch_scope import guard_body_branches, guard_record, scoped_branch_query
from app.database import get_db
from app.dependencies import require_permission
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

router = APIRouter(prefix="/transfers", tags=["Transfers"], dependencies=[Depends(guard_body_branches)])


@router.get(
    "",
    response_model=list[StockTransferResponse],
    status_code=status.HTTP_200_OK,
)
async def list_transfers(
    branch_id: UUID | None = Depends(scoped_branch_query),
    transfer_status: str | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("inventory.transfers.view")),
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
    request: Request,
    data: CreateStockTransferRequest,
    current_user: User = Depends(
        require_permission("inventory.transfers.create")
    ),
    db=Depends(get_db),
):
    return await run_idempotent(
        request,
        business_id=current_user.business_id,
        user_id=current_user.id,
        body=data,
        response_model=StockTransferResponse,
        success_status=201,
        handler=lambda: create_stock_transfer(
        db, current_user.business_id, data, current_user.id
    ),
    )


@router.get(
    "/{transfer_id}",
    response_model=StockTransferResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(guard_record("stock_transfers", "transfer_id"))],
)
async def get_transfer(
    transfer_id: UUID,
    current_user: User = Depends(require_permission("inventory.transfers.view")),
    db=Depends(get_db),
):
    return await get_stock_transfer_by_id(
        db, transfer_id, current_user.business_id
    )


@router.post(
    "/{transfer_id}/receive",
    response_model=StockTransferResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(guard_record("stock_transfers", "transfer_id"))],
)
async def receive_transfer(
    request: Request,
    transfer_id: UUID,
    current_user: User = Depends(
        require_permission("inventory.transfers.receive")
    ),
    db=Depends(get_db),
):
    return await run_idempotent(
        request,
        business_id=current_user.business_id,
        user_id=current_user.id,
        body={"transfer_id": str(transfer_id)},
        response_model=StockTransferResponse,
        success_status=200,
        handler=lambda: receive_stock_transfer(
            db,
            transfer_id,
            current_user.business_id,
            current_user.id,
            receiver=current_user,
        ),
    )
