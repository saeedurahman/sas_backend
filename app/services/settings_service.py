"""Application settings services."""

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sales import TaxRate
from app.models.settings import AppSetting
from app.schemas.settings import BulkUpsertSettingsRequest, UpsertSettingRequest
from app.services import tax_settings
from app.services.audit_helper import log_audit
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

# Keys any signed-in POS user may read without settings.view (receipt printing,
# tax display, POS behaviour). Everything else needs settings.view.
POS_READABLE_PREFIXES = ("receipt.", "tax.")
POS_READABLE_SETTINGS = BUSINESS_SETTINGS | SALE_SETTINGS | {"ui.hide_zero_decimals"}


def is_pos_readable_setting(setting_key: str) -> bool:
    return setting_key in POS_READABLE_SETTINGS or setting_key.startswith(
        POS_READABLE_PREFIXES
    )


def can_read_all_settings(permission_keys: set[str]) -> bool:
    return "settings.view" in permission_keys or "settings.manage" in permission_keys


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


async def _upsert_one(
    db: AsyncSession,
    business_id: UUID,
    item: UpsertSettingRequest,
    updated_by: UUID,
) -> AppSetting:
    """Validate, audit (tax price-mode / rounding) and write one setting; no commit."""
    if item.branch_id is not None:
        await verify_branch(db, item.branch_id, business_id)

    key = item.setting_key
    value = item.setting_value
    is_tax_key = key in tax_settings.TAX_SETTING_KEYS
    if is_tax_key:
        if item.branch_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{key} is a business-wide setting and cannot be set per branch",
            )
        value = tax_settings.validate_setting_payload(key, value)
        if key == tax_settings.KEY_DEFAULT_RATE and value["value"] is not None:
            found = (
                await db.execute(
                    select(TaxRate.id).where(
                        TaxRate.id == UUID(value["value"]),
                        TaxRate.business_id == business_id,
                        TaxRate.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if found is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="tax.default_rate_id must be one of this business's tax rates",
                )

    now = _now()
    result = await db.execute(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.setting_key == key,
            _branch_filter(item.branch_id),
            AppSetting.deleted_at.is_(None),
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        old_stored = existing.setting_value
        existing.setting_value = value
        existing.updated_by = updated_by
        existing.updated_at = now
        row = existing
    else:
        old_stored = None
        row = AppSetting(
            id=uuid4(),
            business_id=business_id,
            branch_id=item.branch_id,
            setting_key=key,
            setting_value=value,
            created_by=updated_by,
            updated_by=updated_by,
            created_at=now,
            updated_at=now,
        )
        db.add(row)

    if key in tax_settings.AUDITED_KEYS:
        before = tax_settings.effective_value(key, old_stored)
        after = tax_settings.effective_value(key, value)
        if before != after:
            await log_audit(
                db,
                business_id=business_id,
                user_id=updated_by,
                branch_id=None,
                action="update",
                table_name="app_settings",
                record_id=row.id,
                old_values={"setting_key": key, "value": before},
                new_values={
                    "event": "tax_setting_changed",
                    "setting_key": key,
                    "value": after,
                    "changed_by": str(updated_by),
                    "applies_to": "new_sales_only",
                },
            )
    return row


async def upsert_setting(
    db: AsyncSession,
    business_id: UUID,
    data: UpsertSettingRequest,
    updated_by: UUID,
) -> AppSetting:
    row = await _upsert_one(db, business_id, data, updated_by)
    await db.commit()
    await db.refresh(row)
    return row


async def bulk_upsert_settings(
    db: AsyncSession,
    business_id: UUID,
    data: BulkUpsertSettingsRequest,
    updated_by: UUID,
) -> list[AppSetting]:
    results: list[AppSetting] = []
    for item in data.settings:
        results.append(await _upsert_one(db, business_id, item, updated_by))
        await db.flush()

    await db.commit()
    for setting in results:
        await db.refresh(setting)
    return results


def with_tax_defaults(
    rows: list[AppSetting], business_id: UUID
) -> list[dict[str, Any]]:
    """Setting rows as response dicts, plus the tax/ui keys that have no row yet."""
    out: list[dict[str, Any]] = [
        {
            "id": r.id,
            "business_id": r.business_id,
            "branch_id": r.branch_id,
            "setting_key": r.setting_key,
            "setting_value": r.setting_value,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]
    present = {r.setting_key for r in rows}
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for key, default in tax_settings.DEFAULTS.items():
        if key in present:
            continue
        out.append(
            {
                "id": uuid5(NAMESPACE_URL, f"setting-default:{business_id}:{key}"),
                "business_id": business_id,
                "branch_id": None,
                "setting_key": key,
                "setting_value": {"value": default},
                "updated_at": epoch,
            }
        )
    out.sort(key=lambda d: d["setting_key"])
    return out


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
