from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.base import BaseSchema


class UpsertSettingRequest(BaseSchema):
    setting_key: str = Field(min_length=1, max_length=100)
    setting_value: dict[str, Any]
    branch_id: UUID | None = None


class BulkUpsertSettingsRequest(BaseSchema):
    settings: list[UpsertSettingRequest] = Field(min_length=1)


class MarkNotificationReadRequest(BaseSchema):
    notification_ids: list[UUID] = Field(min_length=1)


class SettingResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID | None = None
    setting_key: str
    setting_value: dict[str, Any]
    updated_at: datetime


class NotificationResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID | None = None
    user_id: UUID | None = None
    notification_type: str
    channel: str
    title: str
    body: str | None = None
    payload_json: dict[str, Any]
    is_read: bool
    read_at: datetime | None = None
    created_at: datetime


class PaginatedNotificationResponse(BaseSchema):
    total: int
    unread_count: int
    skip: int
    limit: int
    items: list[NotificationResponse]
