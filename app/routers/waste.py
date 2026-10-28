from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.idempotency import run_idempotent
from app.branch_scope import guard_body_branches, guard_record, scoped_branch_query
from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.inventory import CreateWasteEntryRequest, WasteEntryResponse
from app.services.waste_service import (
    create_waste_entry,
    get_waste_entries,
    get_waste_entry_by_id,
)

router = APIRouter(prefix="/waste", tags=["Waste"], dependencies=[Depends(guard_body_branches)])


@router.get(
    "",
    response_model=list[WasteEntryResponse],
    status_code=status.HTTP_200_OK,
)
async def list_waste_entries(
    branch_id: UUID | None = Depends(scoped_branch_query),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("inventory.waste.view")),
    db=Depends(get_db),
):
    entries, _ = await get_waste_entries(
        db, current_user.business_id, branch_id, skip, limit
    )
    return entries


@router.post(
    "",
    response_model=WasteEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_waste(
    request: Request,
    data: CreateWasteEntryRequest,
    current_user: User = Depends(require_permission("inventory.waste.create")),
    db=Depends(get_db),
):
    return await run_idempotent(
        request,
        business_id=current_user.business_id,
        user_id=current_user.id,
        body=data,
        response_model=WasteEntryResponse,
        success_status=201,
        handler=lambda: create_waste_entry(
        db, current_user.business_id, data, current_user.id
    ),
    )


@router.get(
    "/{waste_id}",
    response_model=WasteEntryResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(guard_record("waste_entries", "waste_id"))],
)
async def get_waste(
    waste_id: UUID,
    current_user: User = Depends(require_permission("inventory.waste.view")),
    db=Depends(get_db),
):
    return await get_waste_entry_by_id(
        db, waste_id, current_user.business_id
    )
