from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.sales import CreateSaleReturnRequest, SaleReturnResponse
from app.services.return_service import (
    create_sale_return,
    get_sale_return_by_id,
    get_sale_returns,
)

router = APIRouter(prefix="/returns", tags=["Returns"])


@router.get(
    "",
    response_model=list[SaleReturnResponse],
    status_code=status.HTTP_200_OK,
)
async def list_returns(
    branch_id: UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("sales.returns.view")),
    db=Depends(get_db),
):
    returns, _ = await get_sale_returns(
        db, current_user.business_id, branch_id, skip, limit
    )
    return returns


@router.post(
    "",
    response_model=SaleReturnResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_return(
    data: CreateSaleReturnRequest,
    current_user: User = Depends(require_permission("sales.returns.create")),
    db=Depends(get_db),
):
    return await create_sale_return(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{return_id}",
    response_model=SaleReturnResponse,
    status_code=status.HTTP_200_OK,
)
async def get_return(
    return_id: UUID,
    current_user: User = Depends(require_permission("sales.returns.view")),
    db=Depends(get_db),
):
    return await get_sale_return_by_id(
        db, return_id, current_user.business_id
    )
