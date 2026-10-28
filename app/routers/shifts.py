from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.branch_scope import guard_body_record, guard_record, scoped_branch_query
from app.database import get_db
from app.dependencies import (
    allow_in_grace,
    _user_permission_keys,
    _user_role_names,
    get_current_user,
    require_permission,
)
from app.models.user import User
from app.schemas.register import (
    CloseShiftRequest,
    CreateRegisterTransactionRequest,
    OpenShiftRequest,
    RegisterShiftResponse,
    RegisterTransactionResponse,
    ShiftSummaryResponse,
)
from app.services.register_service import (
    add_cash_movement,
    close_shift,
    get_active_shift_for_user,
    get_shift_by_id,
    get_shift_summary,
    get_shifts,
    open_shift,
)

router = APIRouter(prefix="/shifts", tags=["Register Shifts"])


@router.get("", response_model=list[RegisterShiftResponse], status_code=status.HTTP_200_OK)
async def list_shifts(
    branch_id: UUID | None = Depends(scoped_branch_query),
    register_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("shifts.view")),
    db=Depends(get_db),
):
    shifts, _ = await get_shifts(
        db,
        current_user.business_id,
        branch_id,
        register_id,
        status,
        skip,
        limit,
    )
    return shifts


@router.post("/open", response_model=RegisterShiftResponse, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(guard_body_record("cash_register_id", "cash_registers"))],
)
async def open_shift_endpoint(
    data: OpenShiftRequest,
    current_user: User = Depends(require_permission("shifts.open")),
    db=Depends(get_db),
):
    return await open_shift(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/my-active",
    response_model=RegisterShiftResponse | None,
    status_code=status.HTTP_200_OK,
)
async def my_active_shift(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_active_shift_for_user(
        db, current_user.business_id, current_user.id
    )


@router.get(
    "/{shift_id}",
    response_model=RegisterShiftResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(guard_record("register_shifts", "shift_id"))],
)
async def get_shift(
    shift_id: UUID,
    current_user: User = Depends(require_permission("shifts.view")),
    db=Depends(get_db),
):
    return await get_shift_by_id(db, shift_id, current_user.business_id)


@router.post(
    "/{shift_id}/close",
    response_model=ShiftSummaryResponse,
    status_code=status.HTTP_200_OK,
    # Closing a shift stays possible during the read-only grace period.
    dependencies=[Depends(allow_in_grace), Depends(guard_record("register_shifts", "shift_id"))],
)
async def close_shift_endpoint(
    shift_id: UUID,
    data: CloseShiftRequest,
    current_user: User = Depends(require_permission("shifts.close")),
    db=Depends(get_db),
):
    can_manage = "shifts.manage" in _user_permission_keys(current_user) or (
        "owner" in _user_role_names(current_user)
    )
    return await close_shift(
        db,
        current_user.business_id,
        shift_id,
        data,
        current_user.id,
        can_manage_shifts=can_manage,
    )


@router.get(
    "/{shift_id}/summary",
    response_model=ShiftSummaryResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(guard_record("register_shifts", "shift_id"))],
)
async def shift_summary(
    shift_id: UUID,
    current_user: User = Depends(require_permission("shifts.view")),
    db=Depends(get_db),
):
    return await get_shift_summary(
        db, current_user.business_id, shift_id
    )


@router.post(
    "/{shift_id}/cash-movement",
    response_model=RegisterTransactionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(guard_record("register_shifts", "shift_id"))],
)
async def cash_movement(
    shift_id: UUID,
    data: CreateRegisterTransactionRequest,
    current_user: User = Depends(require_permission("shifts.cash_movement")),
    db=Depends(get_db),
):
    return await add_cash_movement(
        db, current_user.business_id, shift_id, data, current_user.id
    )
