"""Expense management services."""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import (
    LedgerEntryTypeEnum,
    PaymentMethodEnum,
    PaymentStatusEnum,
    RegisterTxTypeEnum,
    ShiftStatusEnum,
)
from app.models.expense import Expense, ExpenseCategory, ExpensePayment
from app.models.register import RegisterShift, RegisterTransaction
from app.schemas.expense import (
    CreateExpenseCategoryRequest,
    CreateExpenseRequest,
    UpdateExpenseCategoryRequest,
    UpdateExpenseRequest,
)
from app.services.stock_service import generate_document_number, verify_branch
from app.services.supplier_ledger_service import create_supplier_ledger_entry
from app.services.supplier_service import get_supplier_by_id

_EXPENSE_REF_TYPE = "expense"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_active_shift_for_branch(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
) -> RegisterShift | None:
    result = await db.execute(
        select(RegisterShift).where(
            RegisterShift.business_id == business_id,
            RegisterShift.branch_id == branch_id,
            RegisterShift.status == ShiftStatusEnum.open.value,
            RegisterShift.deleted_at.is_(None),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_expense_categories(
    db: AsyncSession,
    business_id: UUID,
) -> list[ExpenseCategory]:
    result = await db.execute(
        select(ExpenseCategory)
        .where(
            ExpenseCategory.business_id == business_id,
            ExpenseCategory.deleted_at.is_(None),
            ExpenseCategory.parent_id.is_(None),
        )
        .options(
            selectinload(ExpenseCategory.children).selectinload(
                ExpenseCategory.children
            )
        )
        .order_by(ExpenseCategory.name)
    )
    return list(result.scalars().unique().all())


async def get_expense_category_by_id(
    db: AsyncSession,
    category_id: UUID,
    business_id: UUID,
) -> ExpenseCategory:
    result = await db.execute(
        select(ExpenseCategory)
        .where(
            ExpenseCategory.id == category_id,
            ExpenseCategory.business_id == business_id,
            ExpenseCategory.deleted_at.is_(None),
        )
        .options(selectinload(ExpenseCategory.children))
    )
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Expense category not found",
        )
    return category


async def create_expense_category(
    db: AsyncSession,
    business_id: UUID,
    data: CreateExpenseCategoryRequest,
    created_by: UUID,
) -> ExpenseCategory:
    if data.parent_id is not None:
        await get_expense_category_by_id(db, data.parent_id, business_id)
    now = _now()
    category = ExpenseCategory(
        business_id=business_id,
        name=data.name,
        parent_id=data.parent_id,
        is_active=data.is_active,
        created_by=created_by,
        updated_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return category


async def update_expense_category(
    db: AsyncSession,
    category_id: UUID,
    business_id: UUID,
    data: UpdateExpenseCategoryRequest,
    updated_by: UUID,
) -> ExpenseCategory:
    category = await get_expense_category_by_id(db, category_id, business_id)
    now = _now()
    if data.name is not None:
        category.name = data.name
    if data.is_active is not None:
        category.is_active = data.is_active
    category.updated_by = updated_by
    category.updated_at = now
    await db.commit()
    await db.refresh(category)
    return category


async def delete_expense_category(
    db: AsyncSession,
    category_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    category = await get_expense_category_by_id(db, category_id, business_id)
    count_result = await db.execute(
        select(func.count())
        .select_from(Expense)
        .where(
            Expense.expense_category_id == category_id,
            Expense.business_id == business_id,
            Expense.deleted_at.is_(None),
        )
    )
    if count_result.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete category with active expenses",
        )
    now = _now()
    category.deleted_at = now
    category.deleted_by = deleted_by
    category.updated_by = deleted_by
    category.updated_at = now
    await db.commit()


async def get_expenses(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    category_id: UUID | None = None,
    supplier_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[Expense], int]:
    filters = [
        Expense.business_id == business_id,
        Expense.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(Expense.branch_id == branch_id)
    if category_id is not None:
        filters.append(Expense.expense_category_id == category_id)
    if supplier_id is not None:
        filters.append(Expense.supplier_id == supplier_id)
    if date_from is not None:
        filters.append(Expense.expense_date >= date_from)
    if date_to is not None:
        filters.append(Expense.expense_date <= date_to)

    count_result = await db.execute(
        select(func.count()).select_from(Expense).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Expense)
        .where(*filters)
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_expense_by_id(
    db: AsyncSession,
    expense_id: UUID,
    business_id: UUID,
) -> Expense:
    result = await db.execute(
        select(Expense)
        .where(
            Expense.id == expense_id,
            Expense.business_id == business_id,
            Expense.deleted_at.is_(None),
        )
        .options(selectinload(Expense.payments))
    )
    expense = result.scalar_one_or_none()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Expense not found",
        )
    return expense


async def create_expense(
    db: AsyncSession,
    business_id: UUID,
    data: CreateExpenseRequest,
    created_by: UUID,
) -> Expense:
    await verify_branch(db, data.branch_id, business_id)
    await get_expense_category_by_id(db, data.expense_category_id, business_id)
    if data.supplier_id is not None:
        await get_supplier_by_id(db, data.supplier_id, business_id)

    now = _now()
    expense_number = await generate_document_number(
        db,
        business_id,
        "EXP",
        Expense,
        Expense.expense_number,
    )

    expense = Expense(
        business_id=business_id,
        branch_id=data.branch_id,
        expense_category_id=data.expense_category_id,
        supplier_id=data.supplier_id,
        expense_number=expense_number,
        description=data.description,
        expense_date=data.expense_date,
        amount=data.amount,
        tax_amount=data.tax_amount,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(expense)
    await db.flush()

    active_shift = await _get_active_shift_for_branch(
        db, business_id, data.branch_id
    )

    for payment_data in data.payments:
        paid_at = payment_data.paid_at or now
        payment = ExpensePayment(
            business_id=business_id,
            expense_id=expense.id,
            payment_method=payment_data.payment_method.value,
            amount=payment_data.amount,
            status=PaymentStatusEnum.completed.value,
            paid_at=paid_at,
            reference_no=payment_data.reference_no,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(payment)

        if (
            payment_data.payment_method == PaymentMethodEnum.cash
            and active_shift is not None
        ):
            register_tx = RegisterTransaction(
                business_id=business_id,
                register_shift_id=active_shift.id,
                tx_type=RegisterTxTypeEnum.expense.value,
                payment_method=PaymentMethodEnum.cash.value,
                amount=payment_data.amount,
                reference_type=_EXPENSE_REF_TYPE,
                reference_id=expense.id,
                transacted_at=paid_at,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(register_tx)

    if data.supplier_id is not None:
        liability_amount = -(data.amount + data.tax_amount)
        await create_supplier_ledger_entry(
            db,
            business_id,
            data.supplier_id,
            LedgerEntryTypeEnum.sale,
            liability_amount,
            reference_type=_EXPENSE_REF_TYPE,
            reference_id=expense.id,
            created_by=created_by,
        )

    await db.commit()
    return await get_expense_by_id(db, expense.id, business_id)


async def update_expense(
    db: AsyncSession,
    expense_id: UUID,
    business_id: UUID,
    data: UpdateExpenseRequest,
    updated_by: UUID,
) -> Expense:
    expense = await get_expense_by_id(db, expense_id, business_id)
    now = _now()

    if data.description is not None:
        expense.description = data.description
    if data.expense_date is not None:
        expense.expense_date = data.expense_date
    if data.amount is not None:
        expense.amount = data.amount
    if data.tax_amount is not None:
        expense.tax_amount = data.tax_amount

    expense.updated_by = updated_by
    expense.updated_at = now
    await db.commit()
    return await get_expense_by_id(db, expense_id, business_id)


async def delete_expense(
    db: AsyncSession,
    expense_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    expense = await get_expense_by_id(db, expense_id, business_id)
    now = _now()
    expense.deleted_at = now
    expense.deleted_by = deleted_by
    expense.updated_by = deleted_by
    expense.updated_at = now

    for payment in expense.payments:
        if payment.deleted_at is None:
            payment.deleted_at = now
            payment.updated_at = now

    await db.commit()
