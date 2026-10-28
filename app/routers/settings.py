from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.database import get_db
from app.dependencies import (
    _user_permission_keys,
    get_current_user,
    require_permission,
)
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.settings import (
    BulkUpsertSettingsRequest,
    SettingResponse,
    UpsertSettingRequest,
)
from app.services.settings_service import (
    can_read_all_settings,
    is_pos_readable_setting,
    bulk_upsert_settings,
    delete_setting,
    get_setting,
    get_settings,
    upsert_setting,
)

router = APIRouter(prefix="/settings", tags=["Settings"])


@router.get("", response_model=list[SettingResponse], status_code=status.HTTP_200_OK)
async def list_settings(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    settings_rows = await get_settings(db, current_user.business_id, branch_id)
    if can_read_all_settings(_user_permission_keys(current_user)):
        return settings_rows
    return [row for row in settings_rows if is_pos_readable_setting(row.setting_key)]


@router.put("", response_model=SettingResponse, status_code=status.HTTP_200_OK)
async def upsert_setting_endpoint(
    data: UpsertSettingRequest,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    return await upsert_setting(
        db, current_user.business_id, data, current_user.id
    )


@router.put("/bulk", response_model=list[SettingResponse], status_code=status.HTTP_200_OK)
async def bulk_upsert_settings_endpoint(
    data: BulkUpsertSettingsRequest,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    return await bulk_upsert_settings(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{setting_key:path}",
    response_model=SettingResponse,
    status_code=status.HTTP_200_OK,
)
async def get_setting_endpoint(
    setting_key: str,
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    if not can_read_all_settings(
        _user_permission_keys(current_user)
    ) and not is_pos_readable_setting(setting_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: settings.view",
        )
    setting = await get_setting(
        db, current_user.business_id, setting_key, branch_id
    )
    if setting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Setting not found",
        )
    return setting


@router.delete(
    "/{setting_key:path}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_setting_endpoint(
    setting_key: str,
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    await delete_setting(
        db,
        current_user.business_id,
        setting_key,
        branch_id,
        current_user.id,
    )
    return MessageResponse(message="Setting deleted")
