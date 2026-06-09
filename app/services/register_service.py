"""Cash register and shift management services."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import (
    PaymentMethodEnum,
    RegisterTxTypeEnum,
    ShiftStatusEnum,
)
from app.models.register import CashRegister, RegisterShift, RegisterTransaction
from app.schemas.register import (
    CloseShiftRequest,
    CreateCashRegisterRequest,
    CreateRegisterTransactionRequest,
    OpenShiftRequest,
    ShiftSummaryResponse,
    UpdateCashRegisterRequest,
)
from app.services.stock_service import verify_branch

_ZERO = Decimal("0")
_WALLET_METHODS = {PaymentMethodEnum.wallet.value, PaymentMethodEnum.upi.value}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _log_audit(
    db: AsyncSession,
    *,
    business_id: UUID,
    user_id: UUID,
    action: str,
    table_name: str,
    record_id: UUID,
    new_values: dict,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO audit_logs (
                business_id, user_id, action, table_name,
                record_id, new_values
            ) VALUES (
                :business_id, :user_id, CAST(:action AS audit_action_enum),
                :table_name, :record_id, CAST(:new_values AS jsonb)
            )
            """
        ),
        {
            "business_id": business_id,
            "user_id": user_id,
            "action": action,
            "table_name": table_name,
            "record_id": record_id,
            "new_values": json.dumps(new_values),
        },
    )


def _sum_amounts(
    transactions: list[RegisterTransaction],
    *,
    tx_type: RegisterTxTypeEnum | None = None,
    tx_types: set[RegisterTxTypeEnum] | None = None,
    payment_method: PaymentMethodEnum | None = None,
    payment_methods: set[str] | None = None,
    exclude_payment_methods: set[str] | None = None,
) -> Decimal:
    total = _ZERO
    for tx in transactions:
        if tx.deleted_at is not None:
            continue
        if tx_type is not None and tx.tx_type != tx_type.value:
            continue
        if tx_types is not None and tx.tx_type not in {t.value for t in tx_types}:
            continue
        if payment_method is not None and tx.payment_method != payment_method.value:
            continue
        if payment_methods is not None and tx.payment_method not in payment_methods:
            continue
        if (
            exclude_payment_methods is not None
            and tx.payment_method in exclude_payment_methods
        ):
            continue
        total += tx.amount
    return total


def _calculate_expected_cash(
    shift: RegisterShift,
    transactions: list[RegisterTransaction],
) -> Decimal:
    cash_sales = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.sale,
        payment_method=PaymentMethodEnum.cash,
    )
    cash_returns = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.sale_return,
        payment_method=PaymentMethodEnum.cash,
    )
    cash_in = _sum_amounts(transactions, tx_type=RegisterTxTypeEnum.cash_in)
    cash_out = _sum_amounts(transactions, tx_type=RegisterTxTypeEnum.cash_out)
    cash_expenses = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.expense,
        payment_method=PaymentMethodEnum.cash,
    )
    return (
        shift.opening_float
        + cash_sales
        - cash_returns
        + cash_in
        - cash_out
        - cash_expenses
    )


def _build_shift_summary(
    shift: RegisterShift,
    transactions: list[RegisterTransaction],
) -> ShiftSummaryResponse:
    total_cash_sales = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.sale,
        payment_method=PaymentMethodEnum.cash,
    )
    total_card_sales = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.sale,
        payment_method=PaymentMethodEnum.card,
    )
    total_wallet_sales = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.sale,
        payment_methods=_WALLET_METHODS,
    )
    known_sale_methods = {
        PaymentMethodEnum.cash.value,
        PaymentMethodEnum.card.value,
        *_WALLET_METHODS,
    }
    total_other_sales = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.sale,
        exclude_payment_methods=known_sale_methods,
    )
    total_returns = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.sale_return,
    )
    total_cash_in = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.cash_in,
    )
    total_cash_out = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.cash_out,
    )
    total_expenses = _sum_amounts(
        transactions,
        tx_type=RegisterTxTypeEnum.expense,
    )

    if shift.status == ShiftStatusEnum.closed.value and shift.expected_cash is not None:
        expected_cash = shift.expected_cash
    else:
        expected_cash = _calculate_expected_cash(shift, transactions)

    return ShiftSummaryResponse(
        shift_id=shift.id,
        status=shift.status,
        opening_float=shift.opening_float,
        total_cash_sales=total_cash_sales,
        total_card_sales=total_card_sales,
        total_wallet_sales=total_wallet_sales,
        total_other_sales=total_other_sales,
        total_returns=total_returns,
        total_cash_in=total_cash_in,
        total_cash_out=total_cash_out,
        total_expenses=total_expenses,
        expected_cash=expected_cash,
        actual_cash=shift.actual_cash,
        cash_difference=shift.cash_difference,
    )


async def _load_shift_transactions(
    db: AsyncSession,
    shift_id: UUID,
    business_id: UUID,
) -> list[RegisterTransaction]:
    result = await db.execute(
        select(RegisterTransaction).where(
            RegisterTransaction.register_shift_id == shift_id,
            RegisterTransaction.business_id == business_id,
            RegisterTransaction.deleted_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def get_cash_registers(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> list[CashRegister]:
    stmt = select(CashRegister).where(
        CashRegister.business_id == business_id,
        CashRegister.deleted_at.is_(None),
    )
    if branch_id is not None:
        stmt = stmt.where(CashRegister.branch_id == branch_id)
    result = await db.execute(stmt.order_by(CashRegister.name))
    return list(result.scalars().all())


async def get_cash_register_by_id(
    db: AsyncSession,
    register_id: UUID,
    business_id: UUID,
) -> CashRegister:
    result = await db.execute(
        select(CashRegister).where(
            CashRegister.id == register_id,
            CashRegister.business_id == business_id,
            CashRegister.deleted_at.is_(None),
        )
    )
    register = result.scalar_one_or_none()
    if register is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cash register not found",
        )
    return register


async def create_cash_register(
    db: AsyncSession,
    business_id: UUID,
    data: CreateCashRegisterRequest,
    created_by: UUID,
) -> CashRegister:
    await verify_branch(db, data.branch_id, business_id)
    now = _now()
    register = CashRegister(
        business_id=business_id,
        branch_id=data.branch_id,
        name=data.name,
        device_identifier=data.device_identifier,
        is_active=True,
        created_by=created_by,
        updated_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(register)
    await db.commit()
    await db.refresh(register)
    return register


async def update_cash_register(
    db: AsyncSession,
    register_id: UUID,
    business_id: UUID,
    data: UpdateCashRegisterRequest,
    updated_by: UUID,
) -> CashRegister:
    register = await get_cash_register_by_id(db, register_id, business_id)
    now = _now()

    if data.name is not None:
        register.name = data.name
    if data.device_identifier is not None:
        register.device_identifier = data.device_identifier
    if data.is_active is not None:
        register.is_active = data.is_active

    register.updated_by = updated_by
    register.updated_at = now
    await db.commit()
    await db.refresh(register)
    return register


async def get_active_shift(
    db: AsyncSession,
    business_id: UUID,
    cash_register_id: UUID,
) -> RegisterShift | None:
    result = await db.execute(
        select(RegisterShift).where(
            RegisterShift.cash_register_id == cash_register_id,
            RegisterShift.business_id == business_id,
            RegisterShift.status == ShiftStatusEnum.open.value,
            RegisterShift.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def open_shift(
    db: AsyncSession,
    business_id: UUID,
    data: OpenShiftRequest,
    opened_by: UUID,
) -> RegisterShift:
    register = await get_cash_register_by_id(
        db, data.cash_register_id, business_id
    )
    if not register.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Register is inactive",
        )

    existing_register_shift = await get_active_shift(
        db, business_id, data.cash_register_id
    )
    if existing_register_shift is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A shift is already open for this register",
        )

    user_shift_result = await db.execute(
        select(RegisterShift).where(
            RegisterShift.opened_by == opened_by,
            RegisterShift.business_id == business_id,
            RegisterShift.status == ShiftStatusEnum.open.value,
            RegisterShift.deleted_at.is_(None),
        )
    )
    if user_shift_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already have an open shift",
        )

    now = _now()
    shift = RegisterShift(
        business_id=business_id,
        branch_id=register.branch_id,
        cash_register_id=data.cash_register_id,
        opened_by=opened_by,
        status=ShiftStatusEnum.open.value,
        opening_float=data.opening_float,
        opened_at=now,
        notes=data.notes,
        created_by=opened_by,
        created_at=now,
        updated_at=now,
    )
    db.add(shift)
    await db.flush()

    if data.opening_float > _ZERO:
        opening_tx = RegisterTransaction(
            business_id=business_id,
            register_shift_id=shift.id,
            tx_type=RegisterTxTypeEnum.opening_float.value,
            payment_method=PaymentMethodEnum.cash.value,
            amount=data.opening_float,
            transacted_at=now,
            created_by=opened_by,
            created_at=now,
            updated_at=now,
        )
        db.add(opening_tx)

    await _log_audit(
        db,
        business_id=business_id,
        user_id=opened_by,
        action="create",
        table_name="register_shifts",
        record_id=shift.id,
        new_values={
            "event": "shift_opened",
            "register_id": str(data.cash_register_id),
        },
    )

    await db.commit()
    await db.refresh(shift)
    return shift


async def close_shift(
    db: AsyncSession,
    business_id: UUID,
    shift_id: UUID,
    data: CloseShiftRequest,
    closed_by: UUID,
) -> ShiftSummaryResponse:
    result = await db.execute(
        select(RegisterShift)
        .where(
            RegisterShift.id == shift_id,
            RegisterShift.business_id == business_id,
            RegisterShift.deleted_at.is_(None),
        )
        .with_for_update()
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shift not found",
        )
    if shift.status != ShiftStatusEnum.open.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shift is already closed",
        )

    transactions = await _load_shift_transactions(db, shift_id, business_id)
    expected_cash = _calculate_expected_cash(shift, transactions)
    cash_difference = data.actual_cash - expected_cash
    now = _now()

    shift.status = ShiftStatusEnum.closed.value
    shift.closed_at = now
    shift.closed_by = closed_by
    shift.expected_cash = expected_cash
    shift.actual_cash = data.actual_cash
    shift.cash_difference = cash_difference
    shift.updated_by = closed_by
    shift.updated_at = now
    if data.notes is not None:
        shift.notes = data.notes

    if data.actual_cash > _ZERO:
        closing_tx = RegisterTransaction(
            business_id=business_id,
            register_shift_id=shift.id,
            tx_type=RegisterTxTypeEnum.closing_count.value,
            payment_method=PaymentMethodEnum.cash.value,
            amount=data.actual_cash,
            transacted_at=now,
            created_by=closed_by,
            created_at=now,
            updated_at=now,
        )
        db.add(closing_tx)
        transactions.append(closing_tx)

    await _log_audit(
        db,
        business_id=business_id,
        user_id=closed_by,
        action="update",
        table_name="register_shifts",
        record_id=shift.id,
        new_values={
            "event": "shift_closed",
            "expected": str(expected_cash),
            "actual": str(data.actual_cash),
            "difference": str(cash_difference),
        },
    )

    await db.commit()
    await db.refresh(shift)
    return _build_shift_summary(shift, transactions)


async def add_cash_movement(
    db: AsyncSession,
    business_id: UUID,
    shift_id: UUID,
    data: CreateRegisterTransactionRequest,
    created_by: UUID,
) -> RegisterTransaction:
    result = await db.execute(
        select(RegisterShift).where(
            RegisterShift.id == shift_id,
            RegisterShift.business_id == business_id,
            RegisterShift.deleted_at.is_(None),
        )
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shift not found",
        )
    if shift.status != ShiftStatusEnum.open.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shift is not open",
        )

    if data.tx_type not in (
        RegisterTxTypeEnum.cash_in,
        RegisterTxTypeEnum.cash_out,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only cash_in and cash_out allowed",
        )

    now = _now()
    tx = RegisterTransaction(
        business_id=business_id,
        register_shift_id=shift_id,
        tx_type=data.tx_type.value,
        payment_method=PaymentMethodEnum.cash.value,
        amount=data.amount,
        reference_type=data.reference_type.value if data.reference_type else None,
        reference_id=data.reference_id,
        notes=data.notes,
        transacted_at=now,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(tx)
    await db.flush()

    await _log_audit(
        db,
        business_id=business_id,
        user_id=created_by,
        action="create",
        table_name="register_transactions",
        record_id=tx.id,
        new_values={"event": data.tx_type.value},
    )

    await db.commit()
    await db.refresh(tx)
    return tx


async def get_shift_summary(
    db: AsyncSession,
    business_id: UUID,
    shift_id: UUID,
) -> ShiftSummaryResponse:
    shift = await get_shift_by_id(db, shift_id, business_id)
    transactions = [
        tx for tx in shift.transactions if tx.deleted_at is None
    ]
    return _build_shift_summary(shift, transactions)


async def get_shifts(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    register_id: UUID | None = None,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[RegisterShift], int]:
    filters = [
        RegisterShift.business_id == business_id,
        RegisterShift.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(RegisterShift.branch_id == branch_id)
    if register_id is not None:
        filters.append(RegisterShift.cash_register_id == register_id)
    if status is not None:
        filters.append(RegisterShift.status == status)

    count_result = await db.execute(
        select(func.count()).select_from(RegisterShift).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(RegisterShift)
        .where(*filters)
        .options(selectinload(RegisterShift.transactions))
        .order_by(RegisterShift.opened_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def get_shift_by_id(
    db: AsyncSession,
    shift_id: UUID,
    business_id: UUID,
) -> RegisterShift:
    result = await db.execute(
        select(RegisterShift)
        .where(
            RegisterShift.id == shift_id,
            RegisterShift.business_id == business_id,
            RegisterShift.deleted_at.is_(None),
        )
        .options(selectinload(RegisterShift.transactions))
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shift not found",
        )
    return shift
