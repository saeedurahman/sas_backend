"""Application settings services."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import AppSetting
from app.schemas.settings import BulkUpsertSettingsRequest, UpsertSettingRequest
from app.services.stock_service import verify_branch

# ── Predefined setting keys ───────────────────────────────────────────────────

BUSINESS_SETTINGS = {
    "business.name",
    "business.currency",
    "business.timezone",
    "business.tax_inclusive",
    "business.logo_url",
}

RECEIPT_SETTINGS = {
    "receipt.footer_text",
    "receipt.show_tax",
    "receipt.show_logo",
    "receipt.paper_size",
}

INVENTORY_SETTINGS = {
    "inventory.allow_negative_stock",
    "inventory.low_stock_threshold",
    "inventory.fifo_enabled",
}

SALE_SETTINGS = {
    "sale.require_customer",
    "sale.allow_discount",
    "sale.max_discount_pct",
    "sale.require_shift",
}

NOTIFICATION_SETTINGS = {
    "notifications.low_stock",
    "notifications.expiry",
}

PREDEFINED_SETTING_KEYS = (
    BUSINESS_SETTINGS
    | RECEIPT_SETTINGS
    | INVENTORY_SETTINGS
    | SALE_SETTINGS
    | NOTIFICATION_SETTINGS
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _branch_filter(branch_id: UUID | None):
    if branch_id is None:
        return AppSetting.branch_id.is_(None)
    return AppSetting.branch_id == branch_id


async def get_settings(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> list[AppSetting]:
    if branch_id is None:
        result = await db.execute(
            select(AppSetting)
            .where(
                AppSetting.business_id == business_id,
                AppSetting.branch_id.is_(None),
                AppSetting.deleted_at.is_(None),
            )
            .order_by(AppSetting.setting_key)
        )
        return list(result.scalars().all())

    branch_result = await db.execute(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.branch_id == branch_id,
            AppSetting.deleted_at.is_(None),
        )
    )
    branch_settings = list(branch_result.scalars().all())
    branch_keys = {s.setting_key for s in branch_settings}

    business_result = await db.execute(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.branch_id.is_(None),
            AppSetting.deleted_at.is_(None),
        )
        .order_by(AppSetting.setting_key)
    )
    inherited = [
        s for s in business_result.scalars().all() if s.setting_key not in branch_keys
    ]
    merged = branch_settings + inherited
    merged.sort(key=lambda s: s.setting_key)
    return merged


async def get_setting(
    db: AsyncSession,
    business_id: UUID,
    setting_key: str,
    branch_id: UUID | None = None,
) -> AppSetting | None:
    if branch_id is not None:
        result = await db.execute(
            select(AppSetting).where(
                AppSetting.business_id == business_id,
                AppSetting.setting_key == setting_key,
                AppSetting.branch_id == branch_id,
                AppSetting.deleted_at.is_(None),
            )
        )
        branch_setting = result.scalar_one_or_none()
        if branch_setting is not None:
            return branch_setting

    result = await db.execute(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.setting_key == setting_key,
            AppSetting.branch_id.is_(None),
            AppSetting.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def upsert_setting(
    db: AsyncSession,
    business_id: UUID,
    data: UpsertSettingRequest,
    updated_by: UUID,
) -> AppSetting:
    if data.branch_id is not None:
        await verify_branch(db, data.branch_id, business_id)

    now = _now()
    result = await db.execute(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.setting_key == data.setting_key,
            _branch_filter(data.branch_id),
            AppSetting.deleted_at.is_(None),
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        existing.setting_value = data.setting_value
        existing.updated_by = updated_by
        existing.updated_at = now
        await db.commit()
        await db.refresh(existing)
        return existing

    setting = AppSetting(
        business_id=business_id,
        branch_id=data.branch_id,
        setting_key=data.setting_key,
        setting_value=data.setting_value,
        created_by=updated_by,
        updated_by=updated_by,
        created_at=now,
        updated_at=now,
    )
    db.add(setting)
    await db.commit()
    await db.refresh(setting)
    return setting


async def bulk_upsert_settings(
    db: AsyncSession,
    business_id: UUID,
    data: BulkUpsertSettingsRequest,
    updated_by: UUID,
) -> list[AppSetting]:
    results: list[AppSetting] = []
    for item in data.settings:
        if item.branch_id is not None:
            await verify_branch(db, item.branch_id, business_id)

        now = _now()
        result = await db.execute(
            select(AppSetting).where(
                AppSetting.business_id == business_id,
                AppSetting.setting_key == item.setting_key,
                _branch_filter(item.branch_id),
                AppSetting.deleted_at.is_(None),
            )
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            existing.setting_value = item.setting_value
            existing.updated_by = updated_by
            existing.updated_at = now
            results.append(existing)
        else:
            setting = AppSetting(
                business_id=business_id,
                branch_id=item.branch_id,
                setting_key=item.setting_key,
                setting_value=item.setting_value,
                created_by=updated_by,
                updated_by=updated_by,
                created_at=now,
                updated_at=now,
            )
            db.add(setting)
            results.append(setting)

    await db.commit()
    for setting in results:
        await db.refresh(setting)
    return results


async def delete_setting(
    db: AsyncSession,
    business_id: UUID,
    setting_key: str,
    branch_id: UUID | None,
    deleted_by: UUID,
) -> None:
    result = await db.execute(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.setting_key == setting_key,
            _branch_filter(branch_id),
            AppSetting.deleted_at.is_(None),
        )
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Setting not found",
        )
    now = _now()
    setting.deleted_at = now
    setting.deleted_by = deleted_by
    setting.updated_by = deleted_by
    setting.updated_at = now
    await db.commit()


async def get_setting_value(
    db: AsyncSession,
    business_id: UUID,
    setting_key: str,
    default: Any = None,
    branch_id: UUID | None = None,
) -> Any:
    setting = await get_setting(db, business_id, setting_key, branch_id)
    if setting is None:
        return default
    return setting.setting_value
