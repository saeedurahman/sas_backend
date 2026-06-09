from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.register import (
    CashRegisterResponse,
    CreateCashRegisterRequest,
    RegisterShiftResponse,
    UpdateCashRegisterRequest,
)
from app.services.register_service import (
    create_cash_register,
    get_active_shift,
    get_cash_register_by_id,
    get_cash_registers,
    update_cash_register,
)

router = APIRouter(prefix="/registers", tags=["Cash Registers"])


@router.get("", response_model=list[CashRegisterResponse], status_code=status.HTTP_200_OK)
async def list_registers(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_cash_registers(db, current_user.business_id, branch_id)


@router.post("", response_model=CashRegisterResponse, status_code=status.HTTP_201_CREATED)
async def create_register(
    data: CreateCashRegisterRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_cash_register(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{register_id}",
    response_model=CashRegisterResponse,
    status_code=status.HTTP_200_OK,
)
async def get_register(
    register_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_cash_register_by_id(
        db, register_id, current_user.business_id
    )


@router.put(
    "/{register_id}",
    response_model=CashRegisterResponse,
    status_code=status.HTTP_200_OK,
)
async def update_register(
    register_id: UUID,
    data: UpdateCashRegisterRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await update_cash_register(
        db, register_id, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{register_id}/active-shift",
    response_model=RegisterShiftResponse | None,
    status_code=status.HTTP_200_OK,
)
async def get_register_active_shift(
    register_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_active_shift(
        db, current_user.business_id, register_id
    )
