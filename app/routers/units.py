from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.product import (
    CreateUnitConversionRequest,
    CreateUnitRequest,
    UnitConversionResponse,
    UnitResponse,
    UpdateUnitRequest,
)
from app.services.unit_service import (
    create_unit,
    create_unit_conversion,
    delete_unit,
    get_unit_by_id,
    get_unit_conversions,
    get_units,
    update_unit,
)

router = APIRouter(prefix="/units", tags=["Units"])


@router.get("", response_model=list[UnitResponse], status_code=status.HTTP_200_OK)
async def list_units(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_units(db, current_user.business_id)


@router.post("", response_model=UnitResponse, status_code=status.HTTP_201_CREATED)
async def create_unit_endpoint(
    data: CreateUnitRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_unit(db, current_user.business_id, data, current_user.id)


@router.post(
    "/conversions",
    response_model=UnitConversionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversion_endpoint(
    data: CreateUnitConversionRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_unit_conversion(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/conversions",
    response_model=list[UnitConversionResponse],
    status_code=status.HTTP_200_OK,
)
async def list_conversions(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_unit_conversions(db, current_user.business_id)


@router.get(
    "/{unit_id}",
    response_model=UnitResponse,
    status_code=status.HTTP_200_OK,
)
async def get_unit(
    unit_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_unit_by_id(db, unit_id, current_user.business_id)


@router.put(
    "/{unit_id}",
    response_model=UnitResponse,
    status_code=status.HTTP_200_OK,
)
async def update_unit_endpoint(
    unit_id: UUID,
    data: UpdateUnitRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await update_unit(
        db, unit_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/{unit_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_unit_endpoint(
    unit_id: UUID,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    await delete_unit(db, unit_id, current_user.business_id, current_user.id)
    return MessageResponse(message="Unit deleted")
