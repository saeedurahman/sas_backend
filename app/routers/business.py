from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.dependencies import get_current_user, require_owner
from app.models.user import User
from app.schemas.business import (
    BusinessConfigResponse,
    BusinessResponse,
    BusinessTypeResponse,
    UpdateBusinessRequest,
    UpdateConfigRequest,
)
from app.services.business_service import (
    get_all_business_types,
    get_business_by_id,
    update_business_config,
    update_business_info,
)

router = APIRouter(prefix="/business", tags=["business"])


@router.get("/me", response_model=BusinessResponse, status_code=status.HTTP_200_OK)
async def get_my_business(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_business_by_id(db, current_user.business_id)


@router.put("/me", response_model=BusinessResponse, status_code=status.HTTP_200_OK)
async def update_my_business(
    data: UpdateBusinessRequest,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await update_business_info(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/types",
    response_model=list[BusinessTypeResponse],
    status_code=status.HTTP_200_OK,
)
async def list_business_types(db=Depends(get_db)):
    return await get_all_business_types(db)


@router.put(
    "/config",
    response_model=BusinessConfigResponse,
    status_code=status.HTTP_200_OK,
)
async def update_config(
    data: UpdateConfigRequest,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await update_business_config(
        db, current_user.business_id, data.config_json, current_user.id
    )
