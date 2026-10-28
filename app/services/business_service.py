"""Business profile and configuration services."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.business import Branch, Business, BusinessConfig, BusinessType
from app.schemas.business import UpdateBusinessRequest
from app.services.accounting_coa_seed_service import seed_default_chart_of_accounts
from app.services.feature_flags import get_feature_flag


async def get_business_by_id(db: AsyncSession, business_id: UUID) -> Business:
    result = await db.execute(
        select(Business)
        .where(Business.id == business_id, Business.deleted_at.is_(None))
        .options(
            selectinload(Business.business_type),
            selectinload(Business.config),
            selectinload(Business.branches),
        )
    )
    business = result.scalar_one_or_none()
    if business is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business not found",
        )
    return business


async def update_business_info(
    db: AsyncSession,
    business_id: UUID,
    data: UpdateBusinessRequest,
    updated_by: UUID,
) -> Business:
    business = await get_business_by_id(db, business_id)
    now = datetime.now(timezone.utc)

    if data.name is not None:
        business.name = data.name
    if data.legal_name is not None:
        business.legal_name = data.legal_name
    if data.city is not None:
        business.city = data.city
    if data.email is not None:
        business.email = str(data.email)
    if data.phone is not None:
        business.phone = data.phone
    if data.tax_id is not None:
        business.tax_id = data.tax_id
    if data.address_line1 is not None:
        business.address_line1 = data.address_line1
    if data.logo_url is not None:
        business.logo_url = data.logo_url

    business.updated_by = updated_by
    business.updated_at = now
    await db.commit()
    await db.refresh(business)
    return await get_business_by_id(db, business_id)


async def update_business_config(
    db: AsyncSession,
    business_id: UUID,
    new_config: dict[str, Any],
    updated_by: UUID,
) -> BusinessConfig:
    result = await db.execute(
        select(BusinessConfig).where(
            BusinessConfig.business_id == business_id,
            BusinessConfig.deleted_at.is_(None),
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business configuration not found",
        )

    merged = deepcopy(config.config_json or {})
    was_accounting_enabled = get_feature_flag(merged, "enable_accounting")
    merged.update(new_config)
    now_accounting_enabled = get_feature_flag(merged, "enable_accounting")
    now = datetime.now(timezone.utc)
    config.config_json = merged
    config.updated_by = updated_by
    config.updated_at = now
    if now_accounting_enabled and not was_accounting_enabled:
        await seed_default_chart_of_accounts(
            db,
            business_id,
            created_by=updated_by,
        )
    await db.commit()
    await db.refresh(config)
    return config


async def get_all_business_types(db: AsyncSession) -> list[BusinessType]:
    result = await db.execute(
        select(BusinessType)
        .where(BusinessType.is_active.is_(True))
        .order_by(BusinessType.sort_order.asc())
    )
    return list(result.scalars().all())
