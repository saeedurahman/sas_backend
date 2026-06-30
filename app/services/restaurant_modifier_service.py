"""Modifier groups, modifiers, and product linking."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ModifierSelectionTypeEnum
from app.models.restaurant import Modifier, ModifierGroup, ProductModifierGroup
from app.schemas.restaurant import (
    CreateModifierGroupRequest,
    CreateModifierRequest,
    UpdateModifierGroupRequest,
    UpdateModifierRequest,
)
from app.services.stock_service import verify_product

ModifierSnapshot = dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _effective_min_selections(group: ModifierGroup) -> int:
    minimum = group.min_selections
    if group.is_required and minimum < 1:
        return 1
    return minimum


def validate_group_selection_count(
    group: ModifierGroup,
    selected_count: int,
) -> None:
    minimum = _effective_min_selections(group)
    if selected_count < minimum:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Modifier group '{group.name}' requires at least "
                f"{minimum} selection(s), got {selected_count}"
            ),
        )

    if group.max_selections is not None and selected_count > group.max_selections:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Modifier group '{group.name}' allows at most "
                f"{group.max_selections} selection(s), got {selected_count}"
            ),
        )

    if (
        group.selection_type == ModifierSelectionTypeEnum.single.value
        and selected_count > 1
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Modifier group '{group.name}' allows only one selection"
            ),
        )


async def _get_modifier_group(
    db: AsyncSession,
    group_id: UUID,
    business_id: UUID,
    *,
    with_modifiers: bool = False,
) -> ModifierGroup:
    stmt = select(ModifierGroup).where(
        ModifierGroup.id == group_id,
        ModifierGroup.business_id == business_id,
        ModifierGroup.deleted_at.is_(None),
    )
    if with_modifiers:
        stmt = stmt.options(selectinload(ModifierGroup.modifiers))
    result = await db.execute(stmt)
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Modifier group not found",
        )
    return group


async def _get_modifier(
    db: AsyncSession,
    modifier_id: UUID,
    business_id: UUID,
) -> Modifier:
    result = await db.execute(
        select(Modifier).where(
            Modifier.id == modifier_id,
            Modifier.business_id == business_id,
            Modifier.deleted_at.is_(None),
        )
    )
    modifier = result.scalar_one_or_none()
    if modifier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Modifier not found",
        )
    return modifier


async def get_modifier_groups(
    db: AsyncSession,
    business_id: UUID,
) -> list[ModifierGroup]:
    result = await db.execute(
        select(ModifierGroup)
        .where(
            ModifierGroup.business_id == business_id,
            ModifierGroup.deleted_at.is_(None),
        )
        .options(selectinload(ModifierGroup.modifiers))
        .order_by(ModifierGroup.sort_order, ModifierGroup.name)
    )
    groups = list(result.scalars().unique().all())
    for group in groups:
        group.modifiers = [
            modifier
            for modifier in group.modifiers
            if modifier.deleted_at is None
        ]
    return groups


async def get_modifier_group_by_id(
    db: AsyncSession,
    group_id: UUID,
    business_id: UUID,
) -> ModifierGroup:
    group = await _get_modifier_group(db, group_id, business_id, with_modifiers=True)
    group.modifiers = [
        modifier for modifier in group.modifiers if modifier.deleted_at is None
    ]
    return group


async def create_modifier_group(
    db: AsyncSession,
    business_id: UUID,
    data: CreateModifierGroupRequest,
    created_by: UUID,
) -> ModifierGroup:
    now = _now()
    group = ModifierGroup(
        business_id=business_id,
        name=data.name.strip(),
        selection_type=data.selection_type.value,
        min_selections=data.min_selections,
        max_selections=data.max_selections,
        is_required=data.is_required,
        sort_order=data.sort_order,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(group)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Modifier group name already exists",
        ) from exc
    await db.refresh(group)
    return await get_modifier_group_by_id(db, group.id, business_id)


async def update_modifier_group(
    db: AsyncSession,
    group_id: UUID,
    business_id: UUID,
    data: UpdateModifierGroupRequest,
    updated_by: UUID,
) -> ModifierGroup:
    group = await _get_modifier_group(db, group_id, business_id)
    now = _now()

    if data.name is not None:
        group.name = data.name.strip()
    if data.selection_type is not None:
        group.selection_type = data.selection_type.value
    if data.min_selections is not None:
        group.min_selections = data.min_selections
    if data.max_selections is not None:
        group.max_selections = data.max_selections
    if data.is_required is not None:
        group.is_required = data.is_required
    if data.sort_order is not None:
        group.sort_order = data.sort_order
    if data.is_active is not None:
        group.is_active = data.is_active

    if (
        group.max_selections is not None
        and group.max_selections < group.min_selections
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_selections must be >= min_selections",
        )
    if (
        group.selection_type == ModifierSelectionTypeEnum.single.value
        and group.max_selections is not None
        and group.max_selections > 1
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="single selection groups cannot have max_selections > 1",
        )

    group.updated_by = updated_by
    group.updated_at = now
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Modifier group name already exists",
        ) from exc
    return await get_modifier_group_by_id(db, group_id, business_id)


async def delete_modifier_group(
    db: AsyncSession,
    group_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    group = await _get_modifier_group(db, group_id, business_id, with_modifiers=True)
    now = _now()
    group.deleted_at = now
    group.deleted_by = deleted_by
    group.updated_at = now
    group.updated_by = deleted_by
    for modifier in group.modifiers:
        if modifier.deleted_at is None:
            modifier.deleted_at = now
            modifier.deleted_by = deleted_by
            modifier.updated_at = now
            modifier.updated_by = deleted_by
    await db.commit()


async def create_modifier(
    db: AsyncSession,
    group_id: UUID,
    business_id: UUID,
    data: CreateModifierRequest,
    created_by: UUID,
) -> Modifier:
    await _get_modifier_group(db, group_id, business_id)
    now = _now()
    modifier = Modifier(
        business_id=business_id,
        modifier_group_id=group_id,
        name=data.name.strip(),
        price_delta=data.price_delta,
        sort_order=data.sort_order,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(modifier)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Modifier name already exists in this group",
        ) from exc
    await db.refresh(modifier)
    return modifier


async def update_modifier(
    db: AsyncSession,
    modifier_id: UUID,
    business_id: UUID,
    data: UpdateModifierRequest,
    updated_by: UUID,
) -> Modifier:
    modifier = await _get_modifier(db, modifier_id, business_id)
    now = _now()

    if data.name is not None:
        modifier.name = data.name.strip()
    if data.price_delta is not None:
        modifier.price_delta = data.price_delta
    if data.sort_order is not None:
        modifier.sort_order = data.sort_order
    if data.is_active is not None:
        modifier.is_active = data.is_active

    modifier.updated_by = updated_by
    modifier.updated_at = now
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Modifier name already exists in this group",
        ) from exc
    await db.refresh(modifier)
    return modifier


async def delete_modifier(
    db: AsyncSession,
    modifier_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    modifier = await _get_modifier(db, modifier_id, business_id)
    now = _now()
    modifier.deleted_at = now
    modifier.deleted_by = deleted_by
    modifier.updated_at = now
    modifier.updated_by = deleted_by
    await db.commit()


async def get_product_modifier_groups(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
) -> list[ModifierGroup]:
    await verify_product(db, product_id, business_id)
    result = await db.execute(
        select(ModifierGroup)
        .join(
            ProductModifierGroup,
            ProductModifierGroup.modifier_group_id == ModifierGroup.id,
        )
        .where(
            ProductModifierGroup.business_id == business_id,
            ProductModifierGroup.product_id == product_id,
            ModifierGroup.business_id == business_id,
            ModifierGroup.deleted_at.is_(None),
        )
        .options(selectinload(ModifierGroup.modifiers))
        .order_by(ModifierGroup.sort_order, ModifierGroup.name)
    )
    groups = list(result.scalars().unique().all())
    for group in groups:
        group.modifiers = [
            modifier
            for modifier in group.modifiers
            if modifier.deleted_at is None and modifier.is_active
        ]
    return groups


async def replace_product_modifier_groups(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
    modifier_group_ids: list[UUID],
    created_by: UUID,
) -> list[ModifierGroup]:
    await verify_product(db, product_id, business_id)

    unique_group_ids = list(dict.fromkeys(modifier_group_ids))
    if unique_group_ids:
        result = await db.execute(
            select(ModifierGroup.id).where(
                ModifierGroup.business_id == business_id,
                ModifierGroup.deleted_at.is_(None),
                ModifierGroup.id.in_(unique_group_ids),
            )
        )
        found_ids = set(result.scalars().all())
        missing = [str(gid) for gid in unique_group_ids if gid not in found_ids]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown modifier groups: {', '.join(missing)}",
            )

    await db.execute(
        delete(ProductModifierGroup).where(
            ProductModifierGroup.business_id == business_id,
            ProductModifierGroup.product_id == product_id,
        )
    )
    for group_id in unique_group_ids:
        db.add(
            ProductModifierGroup(
                business_id=business_id,
                product_id=product_id,
                modifier_group_id=group_id,
                created_by=created_by,
            )
        )
    await db.commit()
    return await get_product_modifier_groups(db, product_id, business_id)


async def validate_line_modifiers(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    selected_modifier_ids: list[UUID],
) -> list[ModifierSnapshot]:
    """Validate modifier selections for a product line and return priced snapshots."""
    await verify_product(db, product_id, business_id)
    linked_groups = await get_product_modifier_groups(db, product_id, business_id)
    linked_group_ids = {group.id for group in linked_groups}
    allowed_modifiers: dict[UUID, Modifier] = {}
    for group in linked_groups:
        for modifier in group.modifiers:
            allowed_modifiers[modifier.id] = modifier

    if len(selected_modifier_ids) != len(set(selected_modifier_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate modifier selections are not allowed",
        )

    selected_modifiers: list[Modifier] = []
    for modifier_id in selected_modifier_ids:
        modifier = allowed_modifiers.get(modifier_id)
        if modifier is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Modifier {modifier_id} is not allowed for this product",
            )
        if not modifier.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Modifier '{modifier.name}' is not active",
            )
        selected_modifiers.append(modifier)

    counts_by_group: dict[UUID, int] = {group_id: 0 for group_id in linked_group_ids}
    for modifier in selected_modifiers:
        counts_by_group[modifier.modifier_group_id] += 1

    for group in linked_groups:
        validate_group_selection_count(group, counts_by_group.get(group.id, 0))

    selected_modifiers.sort(
        key=lambda modifier: (
            next(
                group.sort_order
                for group in linked_groups
                if group.id == modifier.modifier_group_id
            ),
            modifier.sort_order,
            modifier.name,
        )
    )
    return [
        {
            "modifier_id": modifier.id,
            "name": modifier.name,
            "price_delta": modifier.price_delta,
        }
        for modifier in selected_modifiers
    ]
