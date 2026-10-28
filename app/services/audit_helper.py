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
) -> None:
    action = _AUTH_ACTION_MAP.get(event, "login")
    new_values = {"event": event, **(extra or {})}
    await db.execute(
        text(
            """
            INSERT INTO audit_logs (
                business_id, user_id, action, table_name,
                record_id, new_values, ip_address, user_agent
            ) VALUES (
                :business_id, :user_id, CAST(:action AS audit_action_enum),
                'users', :record_id,
                CAST(:new_values AS jsonb),
                CAST(:ip_address AS inet), :user_agent
            )
            """
        ),
        {
            "business_id": business_id,
            "user_id": user_id,
            "action": action,
            "record_id": record_id,
            "new_values": json.dumps(new_values),
            "ip_address": ip_address,
            "user_agent": user_agent,
        },
    )
