"""Auth audit logging — uses audit_action_enum values from deployed DB."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# DB enum: create, update, delete, restore, login, logout, sync
_AUTH_ACTION_MAP = {
    "login_success": "login",
    "login_failed": "login",
    "account_locked": "login",
    "token_refreshed": "login",
    "logout": "logout",
    "refresh_reuse_detected": "login",
    "password_changed": "update",
    "password_reset": "update",
    "pin_changed": "update",
    "pin_reset": "update",
    "user_deactivated": "update",
    "user_reactivated": "update",
    "user_deleted": "delete",
}


async def log_auth_event(
    db: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID | None,
    event: str,
    record_id: UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
    extra: dict[str, Any] | None = None,
    branch_id: UUID | None = None,
) -> None:
    action = _AUTH_ACTION_MAP.get(event, "login")
    new_values = {"event": event, **(extra or {})}
    await db.execute(
        text(
            """
            INSERT INTO audit_logs (
                business_id, user_id, branch_id, action, table_name,
                record_id, new_values, ip_address, user_agent
            ) VALUES (
                :business_id, :user_id, :branch_id, CAST(:action AS audit_action_enum),
                'users', :record_id,
                CAST(:new_values AS jsonb),
                CAST(:ip_address AS inet), :user_agent
            )
            """
        ),
        {
            "business_id": business_id,
            "user_id": user_id,
            "branch_id": branch_id,
            "action": action,
            "record_id": record_id,
            "new_values": json.dumps(new_values),
            "ip_address": ip_address,
            "user_agent": user_agent,
        },
    )


async def log_audit(
    db: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID | None,
    branch_id: UUID | None,
    action: str,
    table_name: str,
    record_id: UUID,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    ip_address: str | None = None,
    device_id: str | None = None,
) -> None:
    """Tenant audit row with actor, branch and before/after (``action``: create/update/delete/sync)."""
    await db.execute(
        text(
            """
            INSERT INTO audit_logs (
                business_id, user_id, branch_id, action, table_name, record_id,
                old_values, new_values, ip_address, device_id
            ) VALUES (
                :business_id, :user_id, :branch_id, CAST(:action AS audit_action_enum),
                :table_name, :record_id,
                CAST(:old_values AS jsonb), CAST(:new_values AS jsonb),
                CAST(:ip_address AS inet), :device_id
            )
            """
        ),
        {
            "business_id": business_id,
            "user_id": user_id,
            "branch_id": branch_id,
            "action": action,
            "table_name": table_name,
            "record_id": record_id,
            "old_values": None if old_values is None else json.dumps(old_values, default=str),
            "new_values": None if new_values is None else json.dumps(new_values, default=str),
            "ip_address": ip_address,
            "device_id": device_id,
        },
    )
