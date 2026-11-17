from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema
from app.services.receipt_layout_config import CHANNELS, normalise_config


def _check_config(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return normalise_config(value)  # raises (-> 422) on unknown keys / bad values


class LayoutCreate(BaseSchema):
    name: str = Field(min_length=1, max_length=100)
    design: str
    paper: str | None = None
    config: dict[str, Any] | None = None

    @field_validator("config")
    @classmethod
    def _config(cls, value):
        return _check_config(value)


class LayoutUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    # A layout can switch design; the config (design-independent) is kept unless a new one is sent.
    design: str | None = None
    paper: str | None = None
    config: dict[str, Any] | None = None
    is_active: bool | None = None

    @field_validator("config")
    @classmethod
    def _config(cls, value):
        return _check_config(value)


class LayoutDuplicate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)


class LayoutResponse(BaseSchema):
    id: UUID
    business_id: UUID
    name: str
    design: str
    paper: str
    config: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AssignmentItem(BaseSchema):
    channel: str
    branch_id: UUID | None = None
    layout_id: UUID | None = None

    @field_validator("channel")
    @classmethod
    def _channel(cls, value: str) -> str:
        if value not in CHANNELS:
            raise ValueError(f"channel must be one of: {', '.join(CHANNELS)}")
        return value


class AssignmentsPut(BaseSchema):
    assignments: list[AssignmentItem] = Field(min_length=1, max_length=60)


class AssignmentResponse(BaseSchema):
    id: UUID
    branch_id: UUID | None = None
    channel: str
    layout_id: UUID
    updated_at: datetime


class PreviewRequest(BaseSchema):
    layout_id: UUID | None = None
    design: str | None = None
    paper: str | None = None
    config: dict[str, Any] | None = None
    sale_id: UUID | None = None
    channel: str = "print"

    @field_validator("config")
    @classmethod
    def _config(cls, value):
        return _check_config(value)

    @field_validator("channel")
    @classmethod
    def _channel(cls, value: str) -> str:
        if value not in CHANNELS:
            raise ValueError(f"channel must be one of: {', '.join(CHANNELS)}")
        return value
