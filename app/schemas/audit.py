from datetime import datetime
from typing import Any
from uuid import UUID

from app.schemas.base import BaseSchema


class AuditLogResponse(BaseSchema):
    """Audit log row for API responses.

    user_name is not a database column — it is populated by the service
    layer from the related user record (joinedload) or a separate lookup.
    """

    id: UUID
    business_id: UUID
    user_id: UUID | None = None
    user_name: str | None = None
    action: str
    table_name: str
    record_id: UUID
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    local_id: UUID | None = None
    server_id: UUID | None = None
    sync_status: str
    created_at: datetime


class PaginatedAuditLogResponse(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[AuditLogResponse]
