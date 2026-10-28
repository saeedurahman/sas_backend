"""Category management services."""

import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.product import Category, Product
from app.schemas.product import CreateCategoryRequest, UpdateCategoryRequest


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "category"


async def _generate_unique_slug(
    db: AsyncSession,
    business_id: UUID,
    base_slug: str,
) -> str:
    slug = base_slug
    suffix = 2
    while True:
        result = await db.execute(
            select(Category.id).where(
                Category.business_id == business_id,
                Category.slug == slug,
                Category.deleted_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            return slug
        slug = f"{base_slug}-{suffix}"
        suffix += 1


async def get_categories(
    db: AsyncSession,
    business_id: UUID,
    parent_id: UUID | None = None,
) -> list[Category]:
    stmt = (
        select(Category)
        .where(
            Category.business_id == business_id,
            Category.deleted_at.is_(None),
        )
        .options(selectinload(Category.children))
        .order_by(Category.sort_order, Category.name)
    )
    if parent_id is None:
        stmt = stmt.where(Category.parent_id.is_(None))
    else:
        stmt = stmt.where(Category.parent_id == parent_id)
    result = await db.execute(stmt)
    return list(result.scalars().unique().all())


async def get_category_by_id(
    db: AsyncSession,
    category_id: UUID,
    business_id: UUID,
) -> Category:
    result = await db.execute(
        select(Category)
        .where(
            Category.id == category_id,
            Category.business_id == business_id,
            Category.deleted_at.is_(None),
        )
        .options(selectinload(Category.children))
    )
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )
    return category


async def create_category(
    db: AsyncSession,
    business_id: UUID,
    data: CreateCategoryRequest,
    created_by: UUID,
) -> Category:
    if data.parent_id is not None:
        await get_category_by_id(db, data.parent_id, business_id)

    now = datetime.now(timezone.utc)
    base_slug = data.slug if data.slug else _slugify(data.name)
    slug = await _generate_unique_slug(db, business_id, base_slug)

    category = Category(
        business_id=business_id,
        parent_id=data.parent_id,
        name=data.name,
        slug=slug,
        sort_order=data.sort_order,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return await get_category_by_id(db, category.id, business_id)


async def update_category(
    db: AsyncSession,
    category_id: UUID,
    business_id: UUID,
    data: UpdateCategoryRequest,
    updated_by: UUID,
) -> Category:
    category = await get_category_by_id(db, category_id, business_id)
    now = datetime.now(timezone.utc)

    if data.parent_id is not None:
        if data.parent_id == category_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Category cannot be its own parent",
            )
        await get_category_by_id(db, data.parent_id, business_id)
        category.parent_id = data.parent_id
    if data.name is not None:
        category.name = data.name
    if data.sort_order is not None:
        category.sort_order = data.sort_order
    if data.is_active is not None:
        category.is_active = data.is_active

    category.updated_by = updated_by
    category.updated_at = now
    await db.commit()
    return await get_category_by_id(db, category_id, business_id)


async def delete_category(
    db: AsyncSession,
    category_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    category = await get_category_by_id(db, category_id, business_id)

    count_result = await db.execute(
        select(func.count())
        .select_from(Product)
        .where(
            Product.category_id == category_id,
            Product.business_id == business_id,
            Product.deleted_at.is_(None),
            Product.is_active.is_(True),
        )
    )
    if count_result.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete category with active products",
        )

    now = datetime.now(timezone.utc)
    category.deleted_at = now
    category.deleted_by = deleted_by
    category.updated_at = now
    category.updated_by = deleted_by
    await db.commit()
