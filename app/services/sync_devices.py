"""Device registry for offline sync: register on first contact, revoke, list."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.models.sync import SyncDevice

DEVICE_REVOKED = "device_revoked"


def device_revoked_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": DEVICE_REVOKED,
            "message": "This device has been revoked. Ask the owner to re-enrol it.",
        },
    )


async def touch_device(
    db: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID,
    device_id: str,
    name: str | None = None,
    app_version: str | None = None,
) -> None:
    """Register or refresh the device; 403 ``device_revoked`` when an owner revoked it."""
    row = (
        await db.execute(
            text(
                """
                INSERT INTO sync_devices (business_id, device_id, name, app_version, last_user_id)
                VALUES (:b, :d, :name, :ver, :u)
                ON CONFLICT (business_id, device_id) DO UPDATE
                   SET last_seen_at = now(),
                       last_user_id = EXCLUDED.last_user_id,
                       name = COALESCE(EXCLUDED.name, sync_devices.name),
                       app_version = COALESCE(EXCLUDED.app_version, sync_devices.app_version)
                RETURNING revoked_at
                """
            ),
            {"b": business_id, "d": device_id, "name": name, "ver": app_version, "u": user_id},
        )
    ).first()
    await db.commit()
    if row is not None and row.revoked_at is not None:
        raise device_revoked_error()


def rate_limit_device(scope: str, business_id: UUID, device_id: str) -> None:
    rate = (
        settings.rate_limit_sync_push_device
        if scope == "push"
        else settings.rate_limit_sync_pull_device
    )
    enforce_rate_limit(f"sync-{scope}", f"{business_id}:{device_id}", rate)


async def list_devices(db: AsyncSession, business_id: UUID) -> list[SyncDevice]:
    result = await db.execute(
        select(SyncDevice)
        .where(SyncDevice.business_id == business_id)
        .order_by(SyncDevice.last_seen_at.desc())
    )
    return list(result.scalars().all())


async def revoke_device(
    db: AsyncSession, business_id: UUID, device_id: str, revoked_by: UUID
) -> SyncDevice:
    device = (
        await db.execute(
            select(SyncDevice)
            .where(SyncDevice.business_id == business_id, SyncDevice.device_id == device_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if device.revoked_at is None:
        from app.models.base import utc_now
        from app.services.audit_helper import log_audit

        device.revoked_at = utc_now()
        device.revoked_by = revoked_by
        await log_audit(
            db,
            business_id=business_id,
            user_id=revoked_by,
            branch_id=None,
            action="update",
            table_name="sync_devices",
            record_id=device.id,
            new_values={"event": "device_revoked", "device_id": device_id},
            device_id=device_id,
        )
        await db.commit()
    return device
