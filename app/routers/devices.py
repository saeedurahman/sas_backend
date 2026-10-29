"""Registered POS devices (owner only): list and revoke."""

from fastapi import APIRouter, Depends

from app.database import get_db
from app.dependencies import require_owner
from app.models.user import User
from app.schemas.sync import DeviceResponse
from app.services import sync_devices

router = APIRouter(prefix="/devices", tags=["Devices"])


@router.get("", response_model=list[DeviceResponse])
async def list_devices(current_user: User = Depends(require_owner), db=Depends(get_db)):
    return await sync_devices.list_devices(db, current_user.business_id)


@router.post("/{device_id}/revoke", response_model=DeviceResponse)
async def revoke_device(
    device_id: str,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    """The device gets 403 ``device_revoked`` on its next pull/push."""
    return await sync_devices.revoke_device(
        db, current_user.business_id, device_id, current_user.id
    )
