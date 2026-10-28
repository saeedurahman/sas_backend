"""Branch management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Branch
from app.schemas.business import CreateBranchRequest, UpdateBranchRequest

BRANCH_LIMITS = {
    "trial": 1,
    "basic": 1,
    "growth": 3,
    "pro": 5,
}


async def get_branches_for_business(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> list[Branch]:
    stmt = select(Branch).where(
        Branch.business_id == business_id,
        Branch.deleted_at.is_(None),
        Branch.is_active.is_(True),
    )
    if branch_id is not None:
        stmt = stmt.where(Branch.id == branch_id)
    result = await db.execute(stmt.order_by(Branch.is_head_office.desc(), Branch.name))
    return list(result.scalars().all())


async def create_new_branch(
    db: AsyncSession,
    business_id: UUID,
    subscription_plan: str,
    data: CreateBranchRequest,
    created_by: UUID,
) -> Branch:
    limit = BRANCH_LIMITS.get(subscription_plan, 1)
    count_result = await db.execute(
        select(func.count())
        .select_from(Branch)
        .where(
            Branch.business_id == business_id,
            Branch.deleted_at.is_(None),
            Branch.is_active.is_(True),
        )
    )
    active_count = count_result.scalar_one()
    if active_count >= limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Branch limit reached for your plan. "
                "Upgrade to add more branches."
            ),
        )

    now = datetime.now(timezone.utc)
    branch = Branch(
        business_id=business_id,
        name=data.name,
        address_line1=data.address,
        phone=data.phone,
        is_head_office=False,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    return branch


async def get_branch_by_id(
    db: AsyncSession,
    branch_id: UUID,
    business_id: UUID,
) -> Branch:
    result = await db.execute(
        select(Branch).where(
            Branch.id == branch_id,
            Branch.deleted_at.is_(None),
        )
    )
    branch = result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Branch not found",
        )
    if branch.business_id != business_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    return branch


async def update_branch(
    db: AsyncSession,
    branch_id: UUID,
    business_id: UUID,
    data: UpdateBranchRequest,
    updated_by: UUID,
) -> Branch:
    branch = await get_branch_by_id(db, branch_id, business_id)
    now = datetime.now(timezone.utc)

    if data.name is not None:
        branch.name = data.name
    if data.address is not None:
        branch.address_line1 = data.address
    if data.phone is not None:
        branch.phone = data.phone
    if data.is_active is not None:
        branch.is_active = data.is_active

    branch.updated_by = updated_by
    branch.updated_at = now
    await db.commit()
    await db.refresh(branch)
    return branch
