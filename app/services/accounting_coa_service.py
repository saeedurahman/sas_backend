"""Chart of accounts CRUD services."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import ChartOfAccount, JournalLine
from app.schemas.accounting import (
    ChartOfAccountResponse,
    ChartOfAccountTreeNode,
    CreateChartOfAccountRequest,
    UpdateChartOfAccountRequest,
)
from app.services.accounting_coa_seed_service import ensure_default_chart_of_accounts

_VALID_ACCOUNT_TYPES = frozenset({"asset", "liability", "equity", "income", "expense"})
_VALID_ACCOUNT_SUBTYPES = frozenset(
    {
        "cash",
        "bank",
        "accounts_receivable",
        "accounts_payable",
        "inventory",
        "cogs",
        "sales_revenue",
        "tax_payable",
        "other",
    }
)


def _validate_account_type(account_type: str) -> None:
    if account_type not in _VALID_ACCOUNT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid account_type: {account_type}",
        )


def _validate_account_subtype(account_subtype: str | None) -> None:
    if account_subtype is not None and account_subtype not in _VALID_ACCOUNT_SUBTYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid account_subtype: {account_subtype}",
        )


def _to_response(account: ChartOfAccount) -> ChartOfAccountResponse:
    return ChartOfAccountResponse.model_validate(account)


def _build_tree(accounts: list[ChartOfAccount]) -> list[ChartOfAccountTreeNode]:
    nodes: dict[UUID, ChartOfAccountTreeNode] = {}
    for account in accounts:
        nodes[account.id] = ChartOfAccountTreeNode.model_validate(
            {**ChartOfAccountResponse.model_validate(account).model_dump(), "children": []}
        )
    roots: list[ChartOfAccountTreeNode] = []
    for account in accounts:
        node = nodes[account.id]
        if account.parent_id and account.parent_id in nodes:
            nodes[account.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


async def _get_account_row(
    db: AsyncSession,
    account_id: UUID,
    business_id: UUID,
) -> ChartOfAccount:
    result = await db.execute(
        select(ChartOfAccount).where(
            ChartOfAccount.id == account_id,
            ChartOfAccount.business_id == business_id,
            ChartOfAccount.deleted_at.is_(None),
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chart of accounts entry not found",
        )
    return account


async def _assert_unique_account_code(
    db: AsyncSession,
    business_id: UUID,
    account_code: str,
    *,
    exclude_id: UUID | None = None,
) -> None:
    stmt = select(ChartOfAccount.id).where(
        ChartOfAccount.business_id == business_id,
        ChartOfAccount.account_code == account_code,
        ChartOfAccount.deleted_at.is_(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(ChartOfAccount.id != exclude_id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account code already exists: {account_code}",
        )


async def _has_journal_lines(db: AsyncSession, account_id: UUID) -> bool:
    result = await db.execute(
        select(func.count())
        .select_from(JournalLine)
        .where(JournalLine.account_id == account_id)
    )
    return int(result.scalar_one()) > 0


async def _has_active_children(
    db: AsyncSession,
    account_id: UUID,
    business_id: UUID,
) -> bool:
    result = await db.execute(
        select(func.count())
        .select_from(ChartOfAccount)
        .where(
            ChartOfAccount.parent_id == account_id,
            ChartOfAccount.business_id == business_id,
            ChartOfAccount.deleted_at.is_(None),
        )
    )
    return int(result.scalar_one()) > 0


async def _validate_parent(
    db: AsyncSession,
    business_id: UUID,
    parent_id: UUID | None,
    *,
    account_id: UUID | None = None,
) -> None:
    if parent_id is None:
        return
    if account_id is not None and parent_id == account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account cannot be its own parent",
        )
    await _get_account_row(db, parent_id, business_id)
    if account_id is not None:
        await _assert_no_parent_cycle(db, business_id, account_id, parent_id)


async def _assert_no_parent_cycle(
    db: AsyncSession,
    business_id: UUID,
    account_id: UUID,
    parent_id: UUID,
) -> None:
    to_visit = [account_id]
    while to_visit:
        current = to_visit.pop()
        result = await db.execute(
            select(ChartOfAccount.id).where(
                ChartOfAccount.business_id == business_id,
                ChartOfAccount.parent_id == current,
                ChartOfAccount.deleted_at.is_(None),
            )
        )
        for descendant_id in result.scalars().all():
            if str(descendant_id) == str(parent_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Parent assignment would create a cycle in chart of accounts hierarchy",
                )
            to_visit.append(descendant_id)


async def list_chart_of_accounts(
    db: AsyncSession,
    business_id: UUID,
    *,
    active_only: bool = False,
    tree: bool = False,
    created_by: UUID | None = None,
) -> list[ChartOfAccountResponse] | list[ChartOfAccountTreeNode]:
    await ensure_default_chart_of_accounts(db, business_id, created_by=created_by)

    stmt = (
        select(ChartOfAccount)
        .where(
            ChartOfAccount.business_id == business_id,
            ChartOfAccount.deleted_at.is_(None),
        )
        .order_by(ChartOfAccount.account_code)
    )
    if active_only:
        stmt = stmt.where(ChartOfAccount.is_active.is_(True))

    result = await db.execute(stmt)
    accounts = list(result.scalars().all())
    if tree:
        return _build_tree(accounts)
    return [_to_response(account) for account in accounts]


async def get_chart_of_account_by_id(
    db: AsyncSession,
    account_id: UUID,
    business_id: UUID,
) -> ChartOfAccountResponse:
    account = await _get_account_row(db, account_id, business_id)
    return _to_response(account)


async def create_chart_of_account(
    db: AsyncSession,
    business_id: UUID,
    data: CreateChartOfAccountRequest,
    created_by: UUID,
) -> ChartOfAccountResponse:
    _validate_account_type(data.account_type)
    _validate_account_subtype(data.account_subtype)
    await _validate_parent(db, business_id, data.parent_id)
    await _assert_unique_account_code(db, business_id, data.account_code)

    now = datetime.now(timezone.utc)
    account = ChartOfAccount(
        business_id=business_id,
        parent_id=data.parent_id,
        account_code=data.account_code,
        account_name=data.account_name,
        account_type=data.account_type,
        account_subtype=data.account_subtype,
        description=data.description,
        is_system=False,
        is_active=data.is_active,
        created_by=created_by,
        updated_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return _to_response(account)


async def update_chart_of_account(
    db: AsyncSession,
    account_id: UUID,
    business_id: UUID,
    data: UpdateChartOfAccountRequest,
    updated_by: UUID,
) -> ChartOfAccountResponse:
    account = await _get_account_row(db, account_id, business_id)
    has_lines = await _has_journal_lines(db, account_id)
    now = datetime.now(timezone.utc)

    if "parent_id" in data.model_fields_set:
        if data.parent_id is not None:
            await _validate_parent(db, business_id, data.parent_id, account_id=account_id)
            account.parent_id = data.parent_id
        else:
            account.parent_id = None

    if data.account_code is not None:
        if account.is_system:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change account_code on system accounts",
            )
        await _assert_unique_account_code(
            db,
            business_id,
            data.account_code,
            exclude_id=account_id,
        )
        account.account_code = data.account_code

    if data.account_name is not None:
        account.account_name = data.account_name
    if data.description is not None:
        account.description = data.description
    if data.is_active is not None:
        account.is_active = data.is_active

    if data.account_type is not None:
        _validate_account_type(data.account_type)
        if has_lines and data.account_type != account.account_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change account_type on accounts with journal lines",
            )
        account.account_type = data.account_type

    if data.account_subtype is not None:
        _validate_account_subtype(data.account_subtype)
        if has_lines and data.account_subtype != account.account_subtype:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change account_subtype on accounts with journal lines",
            )
        account.account_subtype = data.account_subtype

    account.updated_by = updated_by
    account.updated_at = now
    await db.commit()
    return await get_chart_of_account_by_id(db, account_id, business_id)


async def delete_chart_of_account(
    db: AsyncSession,
    account_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    account = await _get_account_row(db, account_id, business_id)

    if account.is_system:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete system accounts",
        )
    if await _has_active_children(db, account_id, business_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete account with child accounts",
        )
    if await _has_journal_lines(db, account_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete account with journal lines",
        )

    now = datetime.now(timezone.utc)
    account.deleted_at = now
    account.deleted_by = deleted_by
    account.updated_at = now
    account.updated_by = deleted_by
    await db.commit()
