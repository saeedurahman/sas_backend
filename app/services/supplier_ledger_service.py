"""Supplier ledger services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import LedgerEntryTypeEnum
from app.models.expense import SupplierLedger
from app.schemas.expense import SupplierPaymentRequest
from app.services.supplier_service import get_supplier_by_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_supplier_balance(
    db: AsyncSession,
    supplier_id: UUID,
    business_id: UUID,
) -> Decimal:
    await get_supplier_by_id(db, supplier_id, business_id)
    result = await db.execute(
        select(func.coalesce(func.sum(SupplierLedger.amount), 0)).where(
            SupplierLedger.supplier_id == supplier_id,
            SupplierLedger.business_id == business_id,
            SupplierLedger.deleted_at.is_(None),
        )
    )
    balance = result.scalar_one()
    return Decimal(str(balance))


async def get_supplier_ledger(
    db: AsyncSession,
    supplier_id: UUID,
    business_id: UUID,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[SupplierLedger], int]:
    await get_supplier_by_id(db, supplier_id, business_id)
    filters = [
        SupplierLedger.supplier_id == supplier_id,
        SupplierLedger.business_id == business_id,
        SupplierLedger.deleted_at.is_(None),
    ]
    count_result = await db.execute(
        select(func.count()).select_from(SupplierLedger).where(*filters)
    )
    total = count_result.scalar_one()
    result = await db.execute(
        select(SupplierLedger)
        .where(*filters)
        .order_by(SupplierLedger.entry_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def create_supplier_ledger_entry(
    db: AsyncSession,
    business_id: UUID,
    supplier_id: UUID,
    entry_type: LedgerEntryTypeEnum,
    amount: Decimal,
    *,
    reference_type: str | None = None,
    reference_id: UUID | None = None,
    notes: str | None = None,
    created_by: UUID | None = None,
) -> SupplierLedger:
    now = _now()
    entry = SupplierLedger(
        business_id=business_id,
        supplier_id=supplier_id,
        entry_type=entry_type.value,
        amount=amount,
        reference_type=reference_type,
        reference_id=reference_id,
        entry_at=now,
        notes=notes,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(entry)
    await db.flush()
    return entry


async def record_supplier_payment(
    db: AsyncSession,
    business_id: UUID,
    supplier_id: UUID,
    data: SupplierPaymentRequest,
    created_by: UUID,
) -> SupplierLedger:
    await get_supplier_by_id(db, supplier_id, business_id)
    entry = await create_supplier_ledger_entry(
        db,
        business_id,
        supplier_id,
        LedgerEntryTypeEnum.payment,
        data.amount,
        notes=data.notes or data.reference_no,
        created_by=created_by,
    )
    await db.commit()
    await db.refresh(entry)
    return entry
