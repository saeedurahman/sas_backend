"""
Async business registration — single atomic transaction.
Slug stored in business_configs.config_json (no businesses.slug column in DB).
"""

import random
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Branch, Business, BusinessConfig, BusinessType
from app.models.user import Role, User, UserRole
from app.schemas.auth import RegisterBusinessRequest
from app.services.auth_service import hash_password
from app.services.onboarding_presets import build_onboarding_config_row
from app.services.role_permission_seed import ensure_standard_roles_and_permissions


def _slugify(name: str) -> str:
    value = name.lower().strip()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"[\s_]+", "-", value).strip("-")
    return value[:200] or "business"


def business_slug_from_business(business: Business) -> str:
    """Read tenant slug from business_configs.config_json (PIN login identifier)."""
    config = business.config
    if config is None or config.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Business configuration not loaded",
        )
    slug = (config.config_json or {}).get("slug")
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Business slug not configured",
        )
    return str(slug)


async def _slug_exists(db: AsyncSession, slug: str) -> bool:
    result = await db.execute(
        text(
            """
            SELECT 1 FROM business_configs
            WHERE config_json->>'slug' = :slug
              AND deleted_at IS NULL
            LIMIT 1
            """
        ),
        {"slug": slug},
    )
    return result.scalar_one_or_none() is not None


async def _generate_unique_slug(db: AsyncSession, business_name: str) -> str:
    base = _slugify(business_name)
    if not await _slug_exists(db, base):
        return base
    for _ in range(20):
        candidate = f"{base}-{random.randint(1000, 9999)}"
        if not await _slug_exists(db, candidate):
            return candidate
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not generate unique business slug",
    )


async def register_new_business(
    db: AsyncSession,
    data: RegisterBusinessRequest,
) -> tuple[User, Business]:
    async with db.begin():
        result = await db.execute(
            select(BusinessType).where(
                BusinessType.code == data.business_type_code,
                BusinessType.is_active.is_(True),
            )
        )
        business_type = result.scalar_one_or_none()
        if business_type is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Business type '{data.business_type_code}' is not supported",
            )

        phone_check = await db.execute(
            select(User.id).where(
                User.phone == data.owner_phone,
                User.deleted_at.is_(None),
            )
        )
        if phone_check.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Phone number already registered",
            )

        slug = await _generate_unique_slug(db, data.business_name)
        now = datetime.now(timezone.utc)

        business = Business(
            name=data.business_name,
            business_type_id=business_type.id,
            phone=data.owner_phone,
            city=data.city,
            country_code="PK",
            currency_code="PKR",
            timezone="Asia/Karachi",
            subscription_plan="trial",
            subscription_status="trial",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(business)
        await db.flush()

        preset = build_onboarding_config_row(data.business_type_code)
        config_json = preset.pop("config_json")
        config_json["slug"] = slug

        config = BusinessConfig(
            business_id=business.id,
            config_json=config_json,
            enable_restaurant=preset["enable_restaurant"],
            enable_manufacturing=preset["enable_manufacturing"],
            enable_loyalty=preset["enable_loyalty"],
            enable_multi_price_list=preset["enable_multi_price_list"],
            enable_batch_tracking=preset["enable_batch_tracking"],
            enable_expiry_tracking=preset["enable_expiry_tracking"],
            enable_weight_billing=preset["enable_weight_billing"],
            enable_table_management=preset["enable_table_management"],
            enable_kot=preset["enable_kot"],
            enable_offline_mode=preset["enable_offline_mode"],
            enable_accounting=preset["enable_accounting"],
            default_tax_inclusive=preset["default_tax_inclusive"],
            allow_negative_stock=preset["allow_negative_stock"],
            fifo_costing_enabled=preset["fifo_costing_enabled"],
            receipt_prefix=preset["receipt_prefix"],
            invoice_prefix=preset["invoice_prefix"],
            po_prefix=preset["po_prefix"],
            created_at=now,
            updated_at=now,
        )
        db.add(config)

        branch = Branch(
            business_id=business.id,
            name=data.branch_name,
            city=data.city,
            is_head_office=True,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(branch)
        await db.flush()

        user = User(
            business_id=business.id,
            default_branch_id=branch.id,
            full_name=data.owner_name,
            phone=data.owner_phone,
            password_hash=hash_password(data.owner_password),
            status="active",
            is_locked=False,
            failed_login_attempts=0,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        await db.flush()

        await ensure_standard_roles_and_permissions(
            db,
            business.id,
            created_by=user.id,
        )

        role_result = await db.execute(
            select(Role).where(
                Role.business_id == business.id,
                func.lower(Role.name) == "owner",
                Role.deleted_at.is_(None),
            )
        )
        owner_role = role_result.scalar_one_or_none()
        if owner_role is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Owner role was not created during registration",
            )

        user_role = UserRole(
            business_id=business.id,
            user_id=user.id,
            role_id=owner_role.id,
            branch_id=None,
            created_by=user.id,
            created_at=now,
            updated_at=now,
        )
        db.add(user_role)

        business.created_by = user.id
        config.created_by = user.id
        branch.created_by = user.id

    await db.refresh(user)
    await db.refresh(business)
    return user, business
