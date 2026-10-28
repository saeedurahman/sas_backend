"""Branch management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Branch
from app.schemas.business import CreateBranchRequest, UpdateBranchRequest

async def get_branches_for_business(
    db: AsyncSession,
    business_id: UUID,
    branch_ids: frozenset[UUID] | None = None,
) -> list[Branch]:
    """Active branches; ``branch_ids`` limits the result (None = every branch)."""
    stmt = select(Branch).where(
        Branch.business_id == business_id,
        Branch.deleted_at.is_(None),
        Branch.is_active.is_(True),
    )
    if branch_ids is not None:
        stmt = stmt.where(Branch.id.in_(branch_ids))
    result = await db.execute(stmt.order_by(Branch.is_head_office.desc(), Branch.name))
    return list(result.scalars().all())


async def create_new_branch(
    db: AsyncSession,
    business_id: UUID,
    data: CreateBranchRequest,
    created_by: UUID,
) -> Branch:
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
