"""Supplier management services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import PurchaseOrder, Supplier
from app.schemas.inventory import CreateSupplierRequest, UpdateSupplierRequest


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_suppliers(
    db: AsyncSession,
    business_id: UUID,
    search: str | None = None,
) -> list[Supplier]:
    stmt = select(Supplier).where(
        Supplier.business_id == business_id,
        Supplier.deleted_at.is_(None),
    )
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(Supplier.name.ilike(pattern), Supplier.phone.ilike(pattern))
        )
    result = await db.execute(stmt.order_by(Supplier.name))
    return list(result.scalars().all())


async def get_supplier_by_id(
    db: AsyncSession,
    supplier_id: UUID,
    business_id: UUID,
) -> Supplier:
    result = await db.execute(
        select(Supplier).where(
            Supplier.id == supplier_id,
            Supplier.business_id == business_id,
            Supplier.deleted_at.is_(None),
        )
    )
    supplier = result.scalar_one_or_none()
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Supplier not found",
        )
    return supplier


async def create_supplier(
    db: AsyncSession,
    business_id: UUID,
    data: CreateSupplierRequest,
    created_by: UUID,
) -> Supplier:
    now = _now()
    supplier = Supplier(
        business_id=business_id,
        name=data.name,
        contact_person=data.contact_person,
        email=str(data.email) if data.email else None,
        phone=data.phone,
        tax_id=data.tax_id,
        address_line1=data.address_line1,
        city=data.city,
        payment_terms_days=data.payment_terms_days,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(supplier)
    await db.commit()
    await db.refresh(supplier)
    return supplier


async def update_supplier(
    db: AsyncSession,
    supplier_id: UUID,
    business_id: UUID,
    data: UpdateSupplierRequest,
    updated_by: UUID,
) -> Supplier:
    supplier = await get_supplier_by_id(db, supplier_id, business_id)
    now = _now()

    if data.name is not None:
        supplier.name = data.name
    if data.contact_person is not None:
        supplier.contact_person = data.contact_person
    if data.email is not None:
        supplier.email = str(data.email)
    if data.phone is not None:
        supplier.phone = data.phone
    if data.is_active is not None:
        supplier.is_active = data.is_active
    if data.payment_terms_days is not None:
        supplier.payment_terms_days = data.payment_terms_days

    supplier.updated_by = updated_by
    supplier.updated_at = now
    await db.commit()
    await db.refresh(supplier)
    return supplier


async def delete_supplier(
    db: AsyncSession,
    supplier_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    await get_supplier_by_id(db, supplier_id, business_id)

    count_result = await db.execute(
        select(func.count())
        .select_from(PurchaseOrder)
        .where(
            PurchaseOrder.supplier_id == supplier_id,
            PurchaseOrder.business_id == business_id,
            PurchaseOrder.deleted_at.is_(None),
            PurchaseOrder.status != "cancelled",
        )
    )
    if count_result.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete supplier with active purchase orders",
        )

    now = _now()
    supplier = await get_supplier_by_id(db, supplier_id, business_id)
    supplier.deleted_at = now
    supplier.deleted_by = deleted_by
    supplier.updated_at = now
    supplier.updated_by = deleted_by
    await db.commit()
