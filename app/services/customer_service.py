"""Customer and ledger services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import LedgerEntryTypeEnum, ReferenceTypeEnum
from app.models.sales import Customer, CustomerLedger
from app.schemas.sales import CreateCustomerRequest, UpdateCustomerRequest


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_customers(
    db: AsyncSession,
    business_id: UUID,
    search: str | None = None,
) -> list[Customer]:
    stmt = select(Customer).where(
        Customer.business_id == business_id,
        Customer.deleted_at.is_(None),
    )
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(Customer.name.ilike(pattern), Customer.phone.ilike(pattern))
        )
    result = await db.execute(stmt.order_by(Customer.name))
    return list(result.scalars().all())


async def get_customer_by_id(
    db: AsyncSession,
    customer_id: UUID,
    business_id: UUID,
) -> Customer:
    result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.business_id == business_id,
            Customer.deleted_at.is_(None),
        )
    )
    customer = result.scalar_one_or_none()
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found",
        )
    return customer


async def create_customer(
    db: AsyncSession,
    business_id: UUID,
    data: CreateCustomerRequest,
    created_by: UUID,
) -> Customer:
    now = _now()
    customer = Customer(
        business_id=business_id,
        name=data.name,
        email=str(data.email) if data.email else None,
        phone=data.phone,
        tax_id=data.tax_id,
        address_line1=data.address_line1,
        city=data.city,
        credit_limit=data.credit_limit,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


async def update_customer(
    db: AsyncSession,
    customer_id: UUID,
    business_id: UUID,
    data: UpdateCustomerRequest,
    updated_by: UUID,
) -> Customer:
    customer = await get_customer_by_id(db, customer_id, business_id)
    now = _now()

    if data.name is not None:
        customer.name = data.name
    if data.email is not None:
        customer.email = str(data.email)
    if data.phone is not None:
        customer.phone = data.phone
    if data.credit_limit is not None:
        customer.credit_limit = data.credit_limit
    if data.is_active is not None:
        customer.is_active = data.is_active

    customer.updated_by = updated_by
    customer.updated_at = now
    await db.commit()
    await db.refresh(customer)
    return customer


async def get_customer_balance(
    db: AsyncSession,
    customer_id: UUID,
    business_id: UUID,
) -> Decimal:
    await get_customer_by_id(db, customer_id, business_id)
    result = await db.execute(
        select(func.coalesce(func.sum(CustomerLedger.amount), 0)).where(
            CustomerLedger.customer_id == customer_id,
            CustomerLedger.business_id == business_id,
            CustomerLedger.deleted_at.is_(None),
        )
    )
    return Decimal(str(result.scalar_one()))


async def get_customer_ledger(
    db: AsyncSession,
    customer_id: UUID,
    business_id: UUID,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[CustomerLedger], int]:
    await get_customer_by_id(db, customer_id, business_id)
    filters = [
        CustomerLedger.customer_id == customer_id,
        CustomerLedger.business_id == business_id,
        CustomerLedger.deleted_at.is_(None),
    ]
    count_result = await db.execute(
        select(func.count()).select_from(CustomerLedger).where(*filters)
    )
    total = count_result.scalar_one()
    result = await db.execute(
        select(CustomerLedger)
        .where(*filters)
        .order_by(CustomerLedger.entry_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def create_ledger_entry(
    db: AsyncSession,
    business_id: UUID,
    customer_id: UUID,
    entry_type: LedgerEntryTypeEnum,
    amount: Decimal,
    created_by: UUID,
    reference_type: ReferenceTypeEnum | None = None,
    reference_id: UUID | None = None,
    notes: str | None = None,
) -> CustomerLedger:
    now = _now()
    entry = CustomerLedger(
        business_id=business_id,
        customer_id=customer_id,
        entry_type=entry_type.value,
        amount=amount,
        reference_type=reference_type.value if reference_type else None,
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
