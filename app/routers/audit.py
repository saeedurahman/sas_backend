from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_owner
from app.models.user import User
from app.schemas.audit import AuditLogResponse, PaginatedAuditLogResponse
from app.services.audit_service import get_audit_logs

router = APIRouter(prefix="/audit", tags=["Audit Logs"])


@router.get("", response_model=PaginatedAuditLogResponse, status_code=status.HTTP_200_OK)
async def list_audit_logs(
    user_id: UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    table_name: str | None = Query(default=None),
    record_id: UUID | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    items, total = await get_audit_logs(
        db,
        current_user.business_id,
        user_id=user_id,
        action=action,
        table_name=table_name,
        record_id=record_id,
        date_from=date_from,
        date_to=date_to,
        skip=skip,
        limit=limit,
    )
    return PaginatedAuditLogResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[AuditLogResponse.model_validate(item) for item in items],
    )


@router.get(
    "/record/{table_name}/{record_id}",
    response_model=PaginatedAuditLogResponse,
    status_code=status.HTTP_200_OK,
)
async def audit_logs_for_record(
    table_name: str,
    record_id: UUID,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    items, total = await get_audit_logs(
        db,
        current_user.business_id,
        table_name=table_name,
        record_id=record_id,
        skip=skip,
        limit=limit,
    )
    return PaginatedAuditLogResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[AuditLogResponse.model_validate(item) for item in items],
    )
