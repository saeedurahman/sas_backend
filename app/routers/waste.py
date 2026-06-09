from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.inventory import CreateWasteEntryRequest, WasteEntryResponse
from app.services.waste_service import (
    create_waste_entry,
    get_waste_entries,
    get_waste_entry_by_id,
)

router = APIRouter(prefix="/waste", tags=["Waste"])


@router.get(
    "",
    response_model=list[WasteEntryResponse],
    status_code=status.HTTP_200_OK,
)
async def list_waste_entries(
    branch_id: UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
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
    data: CreateWasteEntryRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_waste_entry(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{waste_id}",
    response_model=WasteEntryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_waste(
    waste_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_waste_entry_by_id(
        db, waste_id, current_user.business_id
    )
