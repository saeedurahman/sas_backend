"""Read-only audit log query service."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.audit import AuditLog


def _audit_log_to_dict(log: AuditLog) -> dict:
    user_name = log.user.full_name if log.user is not None else None
    return {
        "id": log.id,
        "business_id": log.business_id,
        "user_id": log.user_id,
        "user_name": user_name,
        "action": log.action,
        "table_name": log.table_name,
        "record_id": log.record_id,
        "old_values": log.old_values,
        "new_values": log.new_values,
        "ip_address": str(log.ip_address) if log.ip_address else None,
        "user_agent": log.user_agent,
        "local_id": log.local_id,
        "server_id": log.server_id,
        "sync_status": log.sync_status,
        "created_at": log.created_at,
    }


def _build_filters(
    business_id: UUID,
    user_id: UUID | None,
    action: str | None,
    table_name: str | None,
    record_id: UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list:
    filters = [AuditLog.business_id == business_id]

    if user_id is not None:
        filters.append(AuditLog.user_id == user_id)
    if action is not None:
        filters.append(AuditLog.action == action)
    if table_name is not None:
        filters.append(AuditLog.table_name == table_name)
    if record_id is not None:
        filters.append(AuditLog.record_id == record_id)
    if date_from is not None:
        if date_from.tzinfo is None:
            date_from = date_from.replace(tzinfo=timezone.utc)
        filters.append(AuditLog.created_at >= date_from)
    if date_to is not None:
        if date_to.tzinfo is None:
            date_to = date_to.replace(tzinfo=timezone.utc)
        filters.append(AuditLog.created_at < date_to + timedelta(days=1))

    return filters


async def get_audit_logs(
    db: AsyncSession,
    business_id: UUID,
    user_id: UUID | None = None,
    action: str | None = None,
    table_name: str | None = None,
    record_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict], int]:
    filters = _build_filters(
        business_id,
        user_id,
        action,
        table_name,
        record_id,
        date_from,
        date_to,
    )

    count_result = await db.execute(
        select(func.count()).select_from(AuditLog).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(AuditLog)
        .where(*filters)
        .options(joinedload(AuditLog.user))
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    logs = list(result.scalars().unique().all())
    return [_audit_log_to_dict(log) for log in logs], total
