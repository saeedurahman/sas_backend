from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class AuditSchema(BaseSchema):
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
