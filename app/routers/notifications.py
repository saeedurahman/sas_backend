from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.settings import (
    MarkNotificationReadRequest,
    NotificationResponse,
    PaginatedNotificationResponse,
)
from app.services.notification_service import (
    check_expiry_alerts,
    check_low_stock_alerts,
    delete_notification,
    get_notifications,
    mark_all_as_read,
    mark_as_read,
)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class UpdatedCountResponse(BaseModel):
    updated: int


class CheckAlertsResponse(BaseModel):
    low_stock: int
    expiry: int


@router.get(
    "",
    response_model=PaginatedNotificationResponse,
    status_code=status.HTTP_200_OK,
)
async def list_notifications(
    is_read: bool | None = Query(default=None),
    notification_type: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    items, total, unread_count = await get_notifications(
        db,
        current_user.business_id,
        user_id=current_user.id,
        is_read=is_read,
        notification_type=notification_type,
        skip=skip,
        limit=limit,
    )
    return PaginatedNotificationResponse(
        total=total,
        unread_count=unread_count,
        skip=skip,
        limit=limit,
        items=[NotificationResponse.model_validate(n) for n in items],
    )


@router.post(
    "/mark-read",
    response_model=UpdatedCountResponse,
    status_code=status.HTTP_200_OK,
)
async def mark_read(
    data: MarkNotificationReadRequest,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    updated = await mark_as_read(
        db,
        current_user.business_id,
        data.notification_ids,
        current_user.id,
    )
    return UpdatedCountResponse(updated=updated)


@router.post(
    "/mark-all-read",
    response_model=UpdatedCountResponse,
    status_code=status.HTTP_200_OK,
)
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    updated = await mark_all_as_read(
        db, current_user.business_id, current_user.id
    )
    return UpdatedCountResponse(updated=updated)


@router.delete(
    "/{notification_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def remove_notification(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await delete_notification(
        db, current_user.business_id, notification_id
    )
    return MessageResponse(message="Notification deleted")


@router.post(
    "/check-alerts",
    response_model=CheckAlertsResponse,
    status_code=status.HTTP_200_OK,
)
async def run_alert_checks(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    low_stock = await check_low_stock_alerts(db, current_user.business_id)
    expiry = await check_expiry_alerts(db, current_user.business_id)
    return CheckAlertsResponse(
        low_stock=len(low_stock),
        expiry=len(expiry),
    )
