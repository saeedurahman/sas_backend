"""Journal entry draft lifecycle services."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.accounting import ChartOfAccount, JournalEntry, JournalLine
from app.models.enums import ReferenceTypeEnum
from app.schemas.accounting import (
    CreateJournalEntryRequest,
    JournalEntryListResponse,
    JournalEntryResponse,
    JournalLineInput,
    JournalLineResponse,
    UpdateJournalEntryRequest,
)
from app.services.stock_service import generate_document_number, verify_branch

_MANUAL_REFERENCE_TYPE = ReferenceTypeEnum.manual.value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _line_totals(lines: list[JournalLine]) -> tuple[Decimal, Decimal]:
    total_debit = sum((line.debit_amount for line in lines), Decimal("0"))
    total_credit = sum((line.credit_amount for line in lines), Decimal("0"))
    return total_debit, total_credit


def _to_line_response(line: JournalLine) -> JournalLineResponse:
    account = line.account
    return JournalLineResponse(
        id=line.id,
        account_id=line.account_id,
        account_code=account.account_code,
        account_name=account.account_name,
        debit_amount=line.debit_amount,
        credit_amount=line.credit_amount,
        description=line.description,
        line_order=line.line_order,
    )


def _to_entry_response(entry: JournalEntry) -> JournalEntryResponse:
    total_debit, total_credit = _line_totals(entry.lines)
    return JournalEntryResponse(
        id=entry.id,
        business_id=entry.business_id,
        branch_id=entry.branch_id,
        entry_number=entry.entry_number,
        status=entry.status,
        entry_date=entry.entry_date,
        description=entry.description,
        reference_type=entry.reference_type,
        reference_id=entry.reference_id,
        posted_at=entry.posted_at,
        lines=[_to_line_response(line) for line in entry.lines],
        total_debit=total_debit,
        total_credit=total_credit,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        deleted_at=entry.deleted_at,
    )


def _to_list_response(entry: JournalEntry) -> JournalEntryListResponse:
    total_debit, total_credit = _line_totals(entry.lines)
    return JournalEntryListResponse(
        id=entry.id,
        business_id=entry.business_id,
        branch_id=entry.branch_id,
        entry_number=entry.entry_number,
        status=entry.status,
        entry_date=entry.entry_date,
        description=entry.description,
        reference_type=entry.reference_type,
        posted_at=entry.posted_at,
        total_debit=total_debit,
        total_credit=total_credit,
        created_at=entry.created_at,
    )


def _assert_draft(entry: JournalEntry, *, action: str) -> None:
    if entry.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot {action} {entry.status} journal entries",
        )


def _assert_balanced_for_post(lines: list[JournalLine]) -> None:
    if len(lines) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Journal entry must have at least two lines to post",
        )
    total_debit, total_credit = _line_totals(lines)
    if total_debit.quantize(Decimal("0.01")) != total_credit.quantize(Decimal("0.01")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Journal entry is not balanced; debits must equal credits",
        )


async def _get_journal_entry_row(
    db: AsyncSession,
    entry_id: UUID,
    business_id: UUID,
) -> JournalEntry:
    result = await db.execute(
        select(JournalEntry)
        .where(
            JournalEntry.id == entry_id,
            JournalEntry.business_id == business_id,
            JournalEntry.deleted_at.is_(None),
        )
        .options(
            selectinload(JournalEntry.lines).selectinload(JournalLine.account),
        )
        .execution_options(populate_existing=True)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Journal entry not found",
        )
    return entry


async def _get_journal_entry_for_update(
    db: AsyncSession,
    entry_id: UUID,
    business_id: UUID,
) -> JournalEntry:
    result = await db.execute(
        select(JournalEntry)
        .where(
            JournalEntry.id == entry_id,
            JournalEntry.business_id == business_id,
            JournalEntry.deleted_at.is_(None),
        )
        .options(
            selectinload(JournalEntry.lines).selectinload(JournalLine.account),
        )
        .with_for_update()
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Journal entry not found",
        )
    return entry


async def _validate_accounts(
    db: AsyncSession,
    business_id: UUID,
    account_ids: set[UUID],
) -> None:
    if not account_ids:
        return
    result = await db.execute(
        select(ChartOfAccount.id).where(
            ChartOfAccount.business_id == business_id,
            ChartOfAccount.id.in_(account_ids),
            ChartOfAccount.deleted_at.is_(None),
            ChartOfAccount.is_active.is_(True),
        )
    )
    found = set(result.scalars().all())
    missing = account_ids - found
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more accounts are invalid or inactive",
        )


async def _replace_journal_lines(
    db: AsyncSession,
    entry: JournalEntry,
    lines: list[JournalLineInput],
    *,
    created_by: UUID,
) -> None:
    await db.execute(
        delete(JournalLine).where(JournalLine.journal_entry_id == entry.id)
    )
    await db.flush()
    db.expire(entry, ["lines"])
    now = _now()
    for index, line_data in enumerate(lines):
        db.add(
            JournalLine(
                business_id=entry.business_id,
                journal_entry_id=entry.id,
                account_id=line_data.account_id,
                debit_amount=line_data.debit_amount,
                credit_amount=line_data.credit_amount,
                description=line_data.description,
                line_order=line_data.line_order if line_data.line_order else index,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
        )
    await db.flush()
    db.expire(entry, ["lines"])


async def list_journal_entries(
    db: AsyncSession,
    business_id: UUID,
    *,
    status_filter: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    branch_id: UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> list[JournalEntryListResponse]:
    filters = [
        JournalEntry.business_id == business_id,
        JournalEntry.deleted_at.is_(None),
    ]
    if status_filter is not None:
        filters.append(JournalEntry.status == status_filter)
    if date_from is not None:
        filters.append(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        filters.append(JournalEntry.entry_date <= date_to)
    if branch_id is not None:
        filters.append(JournalEntry.branch_id == branch_id)

    result = await db.execute(
        select(JournalEntry)
        .where(*filters)
        .options(
            selectinload(JournalEntry.lines).selectinload(JournalLine.account),
        )
        .order_by(JournalEntry.entry_date.desc(), JournalEntry.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    entries = list(result.scalars().unique().all())
    return [_to_list_response(entry) for entry in entries]


async def get_journal_entry_by_id(
    db: AsyncSession,
    entry_id: UUID,
    business_id: UUID,
) -> JournalEntryResponse:
    entry = await _get_journal_entry_row(db, entry_id, business_id)
    return _to_entry_response(entry)


async def create_journal_entry(
    db: AsyncSession,
    business_id: UUID,
    data: CreateJournalEntryRequest,
    created_by: UUID,
) -> JournalEntryResponse:
    account_ids = {line.account_id for line in data.lines}
    await _validate_accounts(db, business_id, account_ids)

    if data.branch_id is not None:
        await verify_branch(db, data.branch_id, business_id)

    now = _now()
    entry_date = data.entry_date or now.date()
    entry_number = await generate_document_number(
        db,
        business_id,
        "JE",
        JournalEntry,
        JournalEntry.entry_number,
    )

    entry = JournalEntry(
        business_id=business_id,
        branch_id=data.branch_id,
        entry_number=entry_number,
        status="draft",
        entry_date=entry_date,
        description=data.description,
        reference_type=_MANUAL_REFERENCE_TYPE,
        created_by=created_by,
        updated_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(entry)
    await db.flush()
    entry_id = entry.id

    await _replace_journal_lines(db, entry, data.lines, created_by=created_by)
    await db.commit()
    db.expire_all()
    return await get_journal_entry_by_id(db, entry_id, business_id)


async def update_journal_entry(
    db: AsyncSession,
    entry_id: UUID,
    business_id: UUID,
    data: UpdateJournalEntryRequest,
    updated_by: UUID,
) -> JournalEntryResponse:
    entry = await _get_journal_entry_row(db, entry_id, business_id)
    _assert_draft(entry, action="update")

    if data.lines is not None:
        account_ids = {line.account_id for line in data.lines}
        await _validate_accounts(db, business_id, account_ids)

    if data.branch_id is not None:
        await verify_branch(db, data.branch_id, business_id)

    now = _now()
    if data.entry_date is not None:
        entry.entry_date = data.entry_date
    if data.description is not None:
        entry.description = data.description
    if data.branch_id is not None:
        entry.branch_id = data.branch_id

    if data.lines is not None:
        await _replace_journal_lines(db, entry, data.lines, created_by=updated_by)

    entry.updated_by = updated_by
    entry.updated_at = now
    await db.commit()
    db.expire_all()
    return await get_journal_entry_by_id(db, entry_id, business_id)


async def delete_journal_entry(
    db: AsyncSession,
    entry_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    entry = await _get_journal_entry_row(db, entry_id, business_id)
    _assert_draft(entry, action="delete")

    now = _now()
    await db.execute(
        delete(JournalLine).where(JournalLine.journal_entry_id == entry.id)
    )
    entry.deleted_at = now
    entry.deleted_by = deleted_by
    entry.updated_at = now
    entry.updated_by = deleted_by
    await db.commit()


async def post_journal_entry(
    db: AsyncSession,
    entry_id: UUID,
    business_id: UUID,
    posted_by: UUID,
) -> JournalEntryResponse:
    entry = await _get_journal_entry_for_update(db, entry_id, business_id)
    _assert_draft(entry, action="post")
    _assert_balanced_for_post(entry.lines)

    account_ids = {line.account_id for line in entry.lines}
    await _validate_accounts(db, business_id, account_ids)

    now = _now()
    entry.status = "posted"
    entry.posted_at = now
    entry.updated_by = posted_by
    entry.updated_at = now
    await db.commit()
    db.expire_all()
    return await get_journal_entry_by_id(db, entry_id, business_id)
