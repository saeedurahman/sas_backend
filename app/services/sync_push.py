"""Idempotent processing of pushed offline ops.

Each op runs in its own transaction: the op's effects, its stored result (``sync_ops``), and
its audit entry commit together, so a crash can never leave an applied op without a result
or the reverse, and a retry of the same ``op_id`` returns the stored result. A failing op
rolls back to its savepoint and never aborts the rest of the batch.

The existing services are reused through ``ReplaySession``: they ``commit()`` and
``rollback()`` the session themselves, which here would end the whole batch's transaction.
The proxy turns commit into flush and rollback into a no-op; the savepoint around each op
does the real rollback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.dependencies import _user_permission_keys, allowed_branch_ids
from app.models.enums import (
    LedgerEntryTypeEnum,
    NotificationChannelEnum,
    NotificationTypeEnum,
    PaymentMethodEnum,
    ReferenceTypeEnum,
    RegisterTxTypeEnum,
    ShiftStatusEnum,
)
from app.models.product import Product, ProductVariation
from app.models.register import RegisterShift, RegisterTransaction
from app.models.sales import Customer, CustomerLedger, Sale, SaleLine, SalePayment
from app.models.sync import SyncOp
from app.models.user import User
from app.schemas.expense import CreateExpenseRequest
from app.schemas.sales import CreateSaleReturnRequest
from app.schemas.sync import (
    CustomerCreatePayload,
    CustomerPaymentPayload,
    ExpenseCreatePayload,
    OpEnvelope,
    PushRequest,
    ReturnCreatePayload,
    SaleCreatePayload,
    SaleVoidPayload,
    ShiftClosePayload,
    ShiftOpenPayload,
)
from app.services import subscription as sub
from app.services.audit_helper import log_audit
from app.services.customer_service import (
    get_customer_balance,
    get_customer_by_id,
    lock_customer,
)
from app.services.expense_service import create_expense
from app.services.fifo_service import (
    allocate_sale_line_fifo,
    create_sale_line_stock_movements,
)
from app.services.notification_service import create_notification
from app.services.price_service import get_product_price
from app.services.pricing_engine import calculate_line_total, determine_sale_status
from app.services.register_service import (
    _calculate_expected_cash,
    _load_shift_transactions,
    get_active_shift,
    get_active_shift_for_user,
    get_cash_register_by_id,
)
from app.services.return_service import create_sale_return
from app.services.sale_service import void_sale
from app.services.stock_service import (
    generate_document_number,
    get_stock_balance,
    lock_stock_positions,
    verify_branch,
)
from app.services.tax_service import resolve_line_tax_rates

logger = logging.getLogger("app.sync")

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
FUTURE_SKEW = timedelta(minutes=5)

# Flags a replayed sale may carry (it is never rejected for them).
FLAG_PRICE = "price_mismatch"
FLAG_TAX = "tax_mismatch"
FLAG_OVERSOLD = "oversold"
FLAG_CREDIT = "credit_limit_exceeded"
FLAG_COST = "cost_estimated"
FLAG_SHIFT = "shift_mismatch"
FLAG_SUBSCRIPTION = "subscription_expired"
ALL_FLAGS = (FLAG_PRICE, FLAG_TAX, FLAG_OVERSOLD, FLAG_CREDIT, FLAG_COST, FLAG_SHIFT, FLAG_SUBSCRIPTION)

# Stable error codes.
E_MALFORMED = "malformed_op"
E_PAYLOAD = "malformed_payload"
E_TOO_LARGE = "payload_too_large"
E_UNSUPPORTED = "unsupported_op_type"
E_TOO_OLD = "occurred_too_old"
E_FUTURE = "occurred_in_future"
E_PERMISSION = "permission_denied"
E_BRANCH = "branch_forbidden"
E_REUSED = "idempotency_key_reused"
E_UNKNOWN_PRODUCT = "unknown_product"
E_UNKNOWN_CUSTOMER = "unknown_customer"
E_UNKNOWN_SHIFT = "unknown_shift"
E_UNKNOWN_REGISTER = "unknown_register"
E_UNKNOWN_SALE = "sale_not_synced"
E_DUP_RECEIPT = "duplicate_receipt"
E_ID_CONFLICT = "id_conflict"
E_STOCK = "insufficient_stock"
E_DEP_MISSING = "dependency_missing"
E_DEP_FAILED = "dependency_failed"
E_DEP_CYCLE = "dependency_cycle"
E_INTERNAL = "internal_error"

# Only these keep working once the subscription lapsed: the money is already taken.
LAPSED_ALLOWED = frozenset({"sale.create", "customer.payment", "shift.close"})

OP_PERMISSIONS = {
    "customer.create": "customers.create",
    "sale.create": "sales.create",
    "sale.void": "sales.void",
    "customer.payment": "customers.update",
    "expense.create": "expenses.create",
    "shift.open": "shifts.open",
    "shift.close": "shifts.close",
    "return.create": "sales.returns.create",
}
PUSH_PERMISSIONS = tuple(sorted(set(OP_PERMISSIONS.values())))


# ── plumbing ─────────────────────────────────────────────────────────────────


class ReplaySession:
    """AsyncSession whose commit() flushes and rollback() does nothing (see module doc)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def commit(self) -> None:
        await self._session.flush()

    async def rollback(self) -> None:
        return None


class OpReject(Exception):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False, status: str = "rejected"
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status


@dataclass
class Outcome:
    server_id: UUID | None = None
    official_number: str | None = None
    flags: list[str] = field(default_factory=list)
    table: str = "sync_ops"
    branch_id: UUID | None = None
    already_applied: bool = False


@dataclass
class Ctx:
    db: AsyncSession
    business_id: UUID
    user_id: UUID
    permissions: frozenset[str]
    branches: frozenset[UUID] | None  # None = every branch
    business_state: sub.SubscriptionState
    device_id: str
    batch_id: str
    now: datetime

    @property
    def lapsed(self) -> bool:
        return self.business_state.status in (sub.EXPIRED, sub.SUSPENDED, sub.CANCELLED)

    def can_access_branch(self, branch_id: UUID) -> bool:
        return self.branches is None or branch_id in self.branches

    def require_branch(self, branch_id: UUID) -> None:
        if not self.can_access_branch(branch_id):
            raise OpReject(E_BRANCH, "You do not have access to this branch")


async def _exists(db, model, row_id: UUID) -> bool:
    """Cheap existence check (``db.get`` would eagerly load the model's selectin relations)."""
    return (await db.execute(select(model.id).where(model.id == row_id))).first() is not None


def _round2(value: Decimal) -> Decimal:
    return value.quantize(_CENT)


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _payload_hash(op: OpEnvelope) -> str:
    return hashlib.sha256(
        _canonical({"type": op.type, "payload": op.payload, "occurred_at": op.occurred_at.isoformat()})
    ).hexdigest()


def _error(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable}


def _result(
    op_id: UUID | None,
    status: str,
    *,
    server_id: UUID | None = None,
    official_number: str | None = None,
    flags: list[str] | None = None,
    error: dict[str, Any] | None = None,
    original_status: str | None = None,
) -> dict[str, Any]:
    return {
        "op_id": str(op_id) if op_id else None,
        "status": status,
        "server_id": str(server_id) if server_id else None,
        "official_number": official_number,
        "flags": flags or [],
        "error": error,
        "original_status": original_status,
    }


def _map_http(exc: HTTPException) -> OpReject:
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
    if exc.status_code == 403:
        return OpReject(E_PERMISSION, detail)
    if exc.status_code == 404:
        return OpReject("not_found", detail)
    return OpReject("invalid_request", detail)


# ── handlers ─────────────────────────────────────────────────────────────────


async def _h_customer_create(ctx: Ctx, db, p: CustomerCreatePayload, op: OpEnvelope) -> Outcome:
    if await _exists(db, Customer, p.id):
        raise OpReject("id_exists", "A customer with this id already exists", status="conflict")
    customer = Customer(
        id=p.id,
        business_id=ctx.business_id,
        name=p.name,
        phone=p.phone,
        email=p.email,
        tax_id=p.tax_id,
        address_line1=p.address_line1,
        city=p.city,
        credit_limit=p.credit_limit,
        is_active=True,
        sync_status="synced",
        created_by=ctx.user_id,
        created_at=op.occurred_at,
        updated_at=ctx.now,
    )
    db.add(customer)
    await db.flush()
    return Outcome(server_id=customer.id, table="customers")


async def _h_shift_open(ctx: Ctx, db, p: ShiftOpenPayload, op: OpEnvelope) -> Outcome:
    try:
        register = await get_cash_register_by_id(db, p.cash_register_id, ctx.business_id)
    except HTTPException:
        raise OpReject(E_UNKNOWN_REGISTER, "Cash register not found", retryable=True) from None
    ctx.require_branch(register.branch_id)
    if not register.is_active:
        raise OpReject("register_inactive", "Register is inactive")
    if await _exists(db, RegisterShift, p.id):
        raise OpReject("id_exists", "A shift with this id already exists", status="conflict")
    if await get_active_shift(db, ctx.business_id, p.cash_register_id) is not None:
        raise OpReject("shift_conflict", "A shift is already open for this register", status="conflict")
    if await get_active_shift_for_user(db, ctx.business_id, ctx.user_id) is not None:
        raise OpReject("shift_conflict", "You already have an open shift", status="conflict")

    shift = RegisterShift(
        id=p.id,
        business_id=ctx.business_id,
        branch_id=register.branch_id,
        cash_register_id=p.cash_register_id,
        opened_by=ctx.user_id,
        status=ShiftStatusEnum.open.value,
        opening_float=p.opening_float,
        opened_at=op.occurred_at,
        notes=p.notes,
        sync_status="synced",
        created_by=ctx.user_id,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    db.add(shift)
    await db.flush()
    if p.opening_float > _ZERO:
        db.add(
            RegisterTransaction(
                business_id=ctx.business_id,
                register_shift_id=shift.id,
                tx_type=RegisterTxTypeEnum.opening_float.value,
                payment_method=PaymentMethodEnum.cash.value,
                amount=p.opening_float,
                transacted_at=op.occurred_at,
                created_by=ctx.user_id,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
        )
        await db.flush()
    return Outcome(server_id=shift.id, table="register_shifts", branch_id=register.branch_id)


async def _h_shift_close(ctx: Ctx, db, p: ShiftClosePayload, op: OpEnvelope) -> Outcome:
    shift = (
        await db.execute(
            select(RegisterShift)
            .where(
                RegisterShift.id == p.shift_id,
                RegisterShift.business_id == ctx.business_id,
                RegisterShift.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if shift is None:
        raise OpReject(E_UNKNOWN_SHIFT, "Shift not found (has its shift.open been synced?)", retryable=True)
    ctx.require_branch(shift.branch_id)
    if shift.opened_by != ctx.user_id and "shifts.manage" not in ctx.permissions:
        raise OpReject(E_PERMISSION, "Only the user who opened this shift can close it")
    if shift.status != ShiftStatusEnum.open.value:
        raise OpReject("shift_already_closed", "Shift is already closed", status="conflict")
    if op.occurred_at < shift.opened_at:
        raise OpReject("occurred_before_open", "Shift cannot close before it opened")

    transactions = await _load_shift_transactions(db, shift.id, ctx.business_id)
    expected = _calculate_expected_cash(shift, transactions)
    shift.status = ShiftStatusEnum.closed.value
    shift.closed_at = op.occurred_at
    shift.closed_by = ctx.user_id
    shift.expected_cash = expected
    shift.actual_cash = p.actual_cash
    shift.cash_difference = p.actual_cash - expected
    shift.updated_by = ctx.user_id
    shift.updated_at = ctx.now
    if p.notes is not None:
        shift.notes = p.notes
    if p.actual_cash > _ZERO:
        db.add(
            RegisterTransaction(
                business_id=ctx.business_id,
                register_shift_id=shift.id,
                tx_type=RegisterTxTypeEnum.closing_count.value,
                payment_method=PaymentMethodEnum.cash.value,
                amount=p.actual_cash,
                transacted_at=op.occurred_at,
                created_by=ctx.user_id,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
        )
    await db.flush()
    return Outcome(server_id=shift.id, table="register_shifts", branch_id=shift.branch_id)


async def _refresh_closed_shift_totals(db, shift: RegisterShift) -> None:
    """A late cash posting into an already-closed shift must show up in its expected cash."""
    if shift.status != ShiftStatusEnum.closed.value:
        return
    await db.flush()
    transactions = await _load_shift_transactions(db, shift.id, shift.business_id)
    expected = _calculate_expected_cash(shift, transactions)
    shift.expected_cash = expected
    if shift.actual_cash is not None:
        shift.cash_difference = shift.actual_cash - expected


async def _lock_shift(db, ctx: Ctx, shift_id: UUID | None) -> RegisterShift | None:
    if shift_id is None:
        return None
    return (
        await db.execute(
            select(RegisterShift)
            .where(
                RegisterShift.id == shift_id,
                RegisterShift.business_id == ctx.business_id,
                RegisterShift.deleted_at.is_(None),
            )
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()


def _shift_covers(shift: RegisterShift, moment: datetime) -> bool:
    if moment < shift.opened_at:
        return False
    return shift.closed_at is None or moment <= shift.closed_at


async def _h_sale_create(ctx: Ctx, db, p: SaleCreatePayload, op: OpEnvelope) -> Outcome:
    sold_at = op.occurred_at

    # -- idempotency by sale id / client receipt ---------------------------------------
    by_id = (await db.execute(select(Sale).where(Sale.id == p.id))).scalar_one_or_none()
    if by_id is not None and by_id.business_id != ctx.business_id:
        raise OpReject(E_ID_CONFLICT, "This sale id belongs to another business")
    other = (
        await db.execute(
            select(Sale).where(
                Sale.business_id == ctx.business_id,
                Sale.client_receipt_no == p.client_receipt_no,
            )
        )
    ).scalar_one_or_none()
    existing = by_id or other
    if existing is not None:
        same = (
            existing.id == p.id
            and existing.client_receipt_no == p.client_receipt_no
            and existing.branch_id == p.branch_id
        )
        if same:
            return Outcome(
                server_id=existing.id,
                official_number=existing.sale_number,
                flags=list(existing.sync_flags or []),
                table="sales",
                branch_id=existing.branch_id,
                already_applied=True,
            )
        raise OpReject(
            E_DUP_RECEIPT,
            f"Receipt {p.client_receipt_no} was already synced with different content",
        )

    ctx.require_branch(p.branch_id)
    try:
        await verify_branch(db, p.branch_id, ctx.business_id)
    except HTTPException:
        raise OpReject("unknown_branch", "Branch not found") from None

    # -- every product must exist (soft-deleted ones still do) -------------------------
    product_ids = {line.product_id for line in p.lines}
    known = set(
        (
            await db.execute(
                select(Product.id).where(
                    Product.id.in_(product_ids), Product.business_id == ctx.business_id
                )
            )
        ).scalars()
    )
    missing = sorted(str(i) for i in product_ids - known)
    if missing:
        raise OpReject(E_UNKNOWN_PRODUCT, f"Unknown product(s): {', '.join(missing)}")
    variation_ids = {line.variation_id for line in p.lines if line.variation_id}
    if variation_ids:
        known_vars = set(
            (
                await db.execute(
                    select(ProductVariation.id).where(
                        ProductVariation.id.in_(variation_ids),
                        ProductVariation.business_id == ctx.business_id,
                    )
                )
            ).scalars()
        )
        if variation_ids - known_vars:
            raise OpReject(E_UNKNOWN_PRODUCT, "Unknown product variation")

    flags: list[str] = []

    def flag(name: str) -> None:
        if name not in flags:
            flags.append(name)

    if ctx.lapsed:
        flag(FLAG_SUBSCRIPTION)

    # -- shift gate (lock order everywhere: shift -> customer -> stock -> layers) ------
    shift = await _lock_shift(db, ctx, p.register_shift_id)
    usable_shift: RegisterShift | None = None
    if shift is None:
        flag(FLAG_SHIFT)
    else:
        if (
            shift.opened_by != ctx.user_id
            or shift.branch_id != p.branch_id
            or not _shift_covers(shift, sold_at)
        ):
            flag(FLAG_SHIFT)
        if shift.opened_by == ctx.user_id and shift.branch_id == p.branch_id:
            usable_shift = shift  # never post cash into somebody else's drawer

    customer: Customer | None = None
    if p.customer_id is not None:
        try:
            customer = await lock_customer(db, p.customer_id, ctx.business_id)
        except HTTPException:
            raise OpReject(
                E_UNKNOWN_CUSTOMER,
                "Customer not found (has its customer.create been synced?)",
                retryable=True,
            ) from None

    # -- frozen money: exactly what the customer was told ------------------------------
    line_totals = [
        _round2(l.qty * l.unit_price - l.discount_amount + l.tax_amount) for l in p.lines
    ]
    total_amount = sum(line_totals, _ZERO)
    total_applied = sum((pay.amount for pay in p.payments), _ZERO)
    credit_amount = sum(
        (pay.amount for pay in p.payments if pay.payment_method == PaymentMethodEnum.credit), _ZERO
    )
    if credit_amount > _ZERO and customer is None:
        raise OpReject(E_PAYLOAD, "customer_id is required for credit payments")

    # -- server-side recomputation, recorded as flags only -----------------------------
    server_rates = await resolve_line_tax_rates(db, ctx.business_id, list(p.lines), frozenset())
    for line, server_rate in zip(p.lines, server_rates, strict=True):
        expected = calculate_line_total(
            line.qty, line.unit_price, line.discount_pct, line.discount_amount, server_rate
        )
        if abs(expected["tax_amount"] - line.tax_amount) > _CENT or line.tax_rate != server_rate:
            flag(FLAG_TAX)
        try:
            catalog = await get_product_price(
                db, ctx.business_id, line.product_id, line.variation_id, p.price_list_id
            )
        except HTTPException:
            catalog = None
        if catalog is not None and _round2(line.unit_price) != _round2(catalog):
            flag(FLAG_PRICE)

    if credit_amount > _ZERO and customer is not None and customer.credit_limit > _ZERO:
        balance = await get_customer_balance(db, customer.id, ctx.business_id)
        projected = balance - credit_amount
        if projected < _ZERO and abs(projected) > customer.credit_limit:
            flag(FLAG_CREDIT)

    # -- stock: negative allowed when configured; layers fall back to last known cost --
    positions = [(l.product_id, l.variation_id) for l in p.lines]
    await lock_stock_positions(db, ctx.business_id, p.branch_id, positions)
    demand: dict[tuple[UUID, UUID | None], Decimal] = {}
    for line in p.lines:
        key = (line.product_id, line.variation_id)
        demand[key] = demand.get(key, _ZERO) + line.qty
    for (product_id, variation_id), qty in demand.items():
        balance = await get_stock_balance(db, ctx.business_id, p.branch_id, product_id, variation_id)
        if balance < qty:
            if not settings.allow_negative_stock_on_sync:
                raise OpReject(E_STOCK, "Insufficient stock for a replayed sale", retryable=True)
            flag(FLAG_OVERSOLD)

    official_number = await generate_document_number(db, ctx.business_id, "INV", Sale, Sale.sale_number)

    snapshots = []
    for line in p.lines:
        cost, consumptions, short = await allocate_sale_line_fifo(
            db, ctx.business_id, p.branch_id, line.product_id, line.variation_id, line.qty
        )
        if short > _ZERO:
            flag(FLAG_COST)
        snapshots.append((cost, consumptions, short))

    sale = Sale(
        id=p.id,
        business_id=ctx.business_id,
        branch_id=p.branch_id,
        customer_id=customer.id if customer else None,
        register_shift_id=usable_shift.id if usable_shift else None,
        price_list_id=p.price_list_id,
        sale_number=official_number,
        sale_type=p.sale_type.value,
        status=determine_sale_status(total_amount, total_applied).value,
        sold_at=sold_at,
        notes=p.notes,
        discount_scheme_id=p.discount_scheme_id,
        sync_status="synced",
        client_receipt_no=p.client_receipt_no,
        device_id=ctx.device_id,
        is_offline=True,
        client_created_at=op.client_created_at,
        synced_at=ctx.now,
        sync_flags=[],
        created_by=ctx.user_id,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    db.add(sale)
    await db.flush()

    created_lines: list[SaleLine] = []
    for index, (line, (cost, _consumptions, short)) in enumerate(
        zip(p.lines, snapshots, strict=True)
    ):
        sale_line = SaleLine(
            id=line.id or uuid4(),
            business_id=ctx.business_id,
            sale_id=sale.id,
            product_id=line.product_id,
            variation_id=line.variation_id,
            qty=line.qty,
            unit_price=line.unit_price,
            discount_pct=line.discount_pct,
            discount_amount=line.discount_amount,
            tax_rate=line.tax_rate,
            tax_amount=line.tax_amount,
            cost_per_unit=cost,
            cost_estimated=short > _ZERO,
            notes=line.notes,
            line_order=line.line_order if line.line_order is not None else index,
            sync_status="synced",
            created_by=ctx.user_id,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        db.add(sale_line)
        await db.flush()
        created_lines.append(sale_line)

    for pay in p.payments:
        paid_at = pay.paid_at or sold_at
        db.add(
            SalePayment(
                business_id=ctx.business_id,
                sale_id=sale.id,
                payment_method=pay.payment_method.value,
                amount=pay.amount,
                tendered_amount=pay.tendered_amount
                if pay.tendered_amount is not None
                else pay.amount + pay.change_given,
                change_given=pay.change_given,
                status="completed",
                reference_no=pay.reference_no,
                paid_at=paid_at,
                sync_status="synced",
                created_by=ctx.user_id,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
        )
        if usable_shift is not None:
            db.add(
                RegisterTransaction(
                    business_id=ctx.business_id,
                    register_shift_id=usable_shift.id,
                    tx_type=RegisterTxTypeEnum.sale.value,
                    payment_method=pay.payment_method.value,
                    amount=pay.amount,
                    reference_type=ReferenceTypeEnum.sale.value,
                    reference_id=sale.id,
                    transacted_at=paid_at,
                    created_by=ctx.user_id,
                    created_at=ctx.now,
                    updated_at=ctx.now,
                )
            )
    await db.flush()

    for sale_line, (_cost, consumptions, _short) in zip(created_lines, snapshots, strict=True):
        await create_sale_line_stock_movements(
            db,
            business_id=ctx.business_id,
            branch_id=p.branch_id,
            product_id=sale_line.product_id,
            variation_id=sale_line.variation_id,
            sale_line_id=sale_line.id,
            consumptions=consumptions,
            created_by=ctx.user_id,
            movement_at=sold_at,
        )

    if customer is not None and credit_amount > _ZERO:
        db.add(
            CustomerLedger(
                business_id=ctx.business_id,
                customer_id=customer.id,
                entry_type=LedgerEntryTypeEnum.sale.value,
                amount=-credit_amount,
                reference_type=ReferenceTypeEnum.sale.value,
                reference_id=sale.id,
                entry_at=sold_at,
                sync_status="synced",
                created_by=ctx.user_id,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
        )
        await db.flush()

    if usable_shift is not None:
        await _refresh_closed_shift_totals(db, usable_shift)

    sale.sync_flags = list(flags)
    await db.flush()

    if flags:
        await create_notification(
            db,
            ctx.business_id,
            NotificationTypeEnum.sync_conflict,
            NotificationChannelEnum.in_app,
            title=f"Offline sale {official_number} needs review",
            body=f"Receipt {p.client_receipt_no} from device {ctx.device_id}: {', '.join(flags)}",
            payload_json={
                "sale_id": str(sale.id),
                "sale_number": official_number,
                "client_receipt_no": p.client_receipt_no,
                "device_id": ctx.device_id,
                "flags": flags,
            },
            branch_id=p.branch_id,
            created_by=ctx.user_id,
        )
        await log_audit(
            db,
            business_id=ctx.business_id,
            user_id=ctx.user_id,
            branch_id=p.branch_id,
            action="sync",
            table_name="sales",
            record_id=sale.id,
            new_values={
                "event": "sync_sale_flagged",
                "flags": flags,
                "sale_number": official_number,
                "client_receipt_no": p.client_receipt_no,
            },
            device_id=ctx.device_id,
        )
    return Outcome(
        server_id=sale.id,
        official_number=official_number,
        flags=flags,
        table="sales",
        branch_id=p.branch_id,
    )


async def _h_sale_void(ctx: Ctx, db, p: SaleVoidPayload, op: OpEnvelope) -> Outcome:
    sale = (
        await db.execute(
            select(Sale).where(
                Sale.id == p.sale_id, Sale.business_id == ctx.business_id, Sale.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if sale is None:
        raise OpReject(E_UNKNOWN_SALE, "Sale has not been synced yet", retryable=True)
    ctx.require_branch(sale.branch_id)
    branch_id, number = sale.branch_id, sale.sale_number
    try:
        await void_sale(db, p.sale_id, ctx.business_id, ctx.user_id)
    except HTTPException as exc:
        raise OpReject("sale_not_voidable", str(exc.detail), status="conflict") from None
    return Outcome(server_id=p.sale_id, official_number=number, table="sales", branch_id=branch_id)


async def _h_customer_payment(ctx: Ctx, db, p: CustomerPaymentPayload, op: OpEnvelope) -> Outcome:
    try:
        await get_customer_by_id(db, p.customer_id, ctx.business_id)
    except HTTPException:
        raise OpReject(
            E_UNKNOWN_CUSTOMER, "Customer not found (has its customer.create been synced?)",
            retryable=True,
        ) from None
    entry_id = p.id or uuid4()
    if await _exists(db, CustomerLedger, entry_id):
        raise OpReject("id_exists", "A ledger entry with this id already exists", status="conflict")

    flags: list[str] = []
    if ctx.lapsed:
        flags.append(FLAG_SUBSCRIPTION)
    notes = " | ".join(
        part
        for part in (
            f"Payment received via {p.payment_method.value}",
            f"Ref: {p.reference_no}" if p.reference_no else None,
            p.notes,
        )
        if part
    )
    db.add(
        CustomerLedger(
            id=entry_id,
            business_id=ctx.business_id,
            customer_id=p.customer_id,
            entry_type=LedgerEntryTypeEnum.payment.value,
            amount=p.amount,
            reference_type=ReferenceTypeEnum.manual.value,
            reference_id=p.customer_id,
            entry_at=op.occurred_at,
            notes=notes,
            sync_status="synced",
            created_by=ctx.user_id,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
    )
    await db.flush()

    if p.payment_method == PaymentMethodEnum.cash and p.register_shift_id is not None:
        shift = await _lock_shift(db, ctx, p.register_shift_id)
        if shift is None or shift.opened_by != ctx.user_id:
            flags.append(FLAG_SHIFT)  # recorded in the ledger, but not in somebody's drawer
        else:
            db.add(
                RegisterTransaction(
                    business_id=ctx.business_id,
                    register_shift_id=shift.id,
                    tx_type=RegisterTxTypeEnum.cash_in.value,
                    payment_method=PaymentMethodEnum.cash.value,
                    amount=p.amount,
                    reference_type=ReferenceTypeEnum.manual.value,
                    reference_id=entry_id,
                    notes=notes,
                    transacted_at=op.occurred_at,
                    created_by=ctx.user_id,
                    created_at=ctx.now,
                    updated_at=ctx.now,
                )
            )
            await _refresh_closed_shift_totals(db, shift)
    return Outcome(server_id=entry_id, flags=flags, table="customer_ledger")


async def _h_expense_create(ctx: Ctx, db, p: ExpenseCreatePayload, op: OpEnvelope) -> Outcome:
    ctx.require_branch(p.branch_id)
    data = p.model_dump(mode="python")
    for payment in data["payments"]:
        payment["paid_at"] = payment["paid_at"] or op.occurred_at
    try:
        request = CreateExpenseRequest.model_validate(data)
        expense = await create_expense(db, ctx.business_id, request, ctx.user_id)
    except ValidationError as exc:
        raise OpReject(E_PAYLOAD, exc.errors()[0]["msg"]) from None
    except HTTPException as exc:
        raise OpReject("invalid_reference", str(exc.detail)) from None
    return Outcome(
        server_id=expense.id,
        official_number=expense.expense_number,
        table="expenses",
        branch_id=p.branch_id,
    )


async def _h_return_create(ctx: Ctx, db, p: ReturnCreatePayload, op: OpEnvelope) -> Outcome:
    ctx.require_branch(p.branch_id)
    if p.sale_id is not None:
        exists = (
            await db.execute(
                select(Sale.id).where(Sale.id == p.sale_id, Sale.business_id == ctx.business_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise OpReject(E_UNKNOWN_SALE, "Original sale has not been synced yet", retryable=True)
    try:
        request = CreateSaleReturnRequest.model_validate(
            {**p.model_dump(mode="python"), "returned_at": op.occurred_at}
        )
        sale_return = await create_sale_return(db, ctx.business_id, request, ctx.user_id)
    except ValidationError as exc:
        raise OpReject(E_PAYLOAD, exc.errors()[0]["msg"]) from None
    except HTTPException as exc:
        detail = str(exc.detail)
        if "no open shift" in detail:
            raise OpReject(
                "no_open_shift", "Returns need the user's open shift at sync time", retryable=True
            ) from None
        raise OpReject("return_rejected", detail, status="conflict") from None
    return Outcome(
        server_id=sale_return.id,
        official_number=sale_return.return_number,
        table="sale_returns",
        branch_id=p.branch_id,
    )


HANDLERS = {
    "customer.create": (CustomerCreatePayload, _h_customer_create),
    "sale.create": (SaleCreatePayload, _h_sale_create),
    "sale.void": (SaleVoidPayload, _h_sale_void),
    "customer.payment": (CustomerPaymentPayload, _h_customer_payment),
    "expense.create": (ExpenseCreatePayload, _h_expense_create),
    "shift.open": (ShiftOpenPayload, _h_shift_open),
    "shift.close": (ShiftClosePayload, _h_shift_close),
    "return.create": (ReturnCreatePayload, _h_return_create),
}


# ── one op ───────────────────────────────────────────────────────────────────


def _gate(ctx: Ctx, op: OpEnvelope) -> None:
    """Subscription, time window and permission checks (cheap, before any write)."""
    state = ctx.business_state
    if state.status == sub.GRACE and op.type not in LAPSED_ALLOWED:
        raise OpReject(
            sub.CODE_GRACE_READONLY,
            "Subscription is in its read-only grace period: only sales, customer payments and "
            "shift closes are accepted",
            retryable=True,
        )
    if ctx.lapsed and op.type not in LAPSED_ALLOWED:
        raise OpReject(
            state.blocked_code or sub.CODE_EXPIRED,
            "Subscription is not active: only sales, customer payments and shift closes are accepted",
            retryable=True,
        )
    oldest = ctx.now - timedelta(days=settings.max_offline_days)
    if op.occurred_at < oldest:
        raise OpReject(
            E_TOO_OLD,
            f"occurred_at is more than {settings.max_offline_days} days in the past",
        )
    if op.occurred_at > ctx.now + FUTURE_SKEW:
        raise OpReject(E_FUTURE, "occurred_at is in the future beyond the allowed clock skew")
    required = OP_PERMISSIONS[op.type]
    if required not in ctx.permissions:
        raise OpReject(E_PERMISSION, f"Permission required: {required}")


def _is_deadlock(exc: DBAPIError) -> bool:
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return code in {"40P01", "40001"}


async def _store(ctx: Ctx, op: OpEnvelope, payload_hash: str, status: str, result: dict) -> None:
    ctx.db.add(
        SyncOp(
            business_id=ctx.business_id,
            op_id=op.op_id,
            device_id=ctx.device_id,
            batch_id=ctx.batch_id,
            user_id=ctx.user_id,
            op_type=op.type,
            status=status,
            payload_hash=payload_hash,
            result=result,
            client_created_at=op.client_created_at,
            occurred_at=op.occurred_at,
        )
    )
    await ctx.db.flush()


async def _run_op(ctx: Ctx, op: OpEnvelope) -> dict[str, Any]:
    db = ctx.db
    payload_hash = _payload_hash(op)
    for attempt in range(3):
        try:
            return await _run_op_once(ctx, op, payload_hash)
        except DBAPIError as exc:
            await db.rollback()
            if _is_deadlock(exc) and attempt < 2:
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            logger.exception("sync op failed", extra={"op_id": str(op.op_id), "op_type": op.type})
            return _result(op.op_id, "rejected", error=_error(E_INTERNAL, "Database error", True))
        except Exception:  # noqa: BLE001 - one bad op must not abort the batch
            await db.rollback()
            logger.exception("sync op crashed", extra={"op_id": str(op.op_id), "op_type": op.type})
            return _result(op.op_id, "rejected", error=_error(E_INTERNAL, "Internal error", True))
    raise AssertionError("unreachable")


async def _run_op_once(ctx: Ctx, op: OpEnvelope, payload_hash: str) -> dict[str, Any]:
    db = ctx.db
    # Serialises concurrent deliveries of the same op (original + retry); released at commit.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"syncop:{ctx.business_id}:{op.op_id}"},
    )
    stored = (
        await db.execute(
            select(SyncOp).where(SyncOp.business_id == ctx.business_id, SyncOp.op_id == op.op_id)
        )
    ).scalar_one_or_none()
    if stored is not None:
        previous = dict(stored.result)
        same = stored.payload_hash == payload_hash
        status = stored.status
        await db.commit()
        if not same:
            return _result(
                op.op_id,
                "rejected",
                error=_error(E_REUSED, "op_id was already used for a different operation"),
            )
        if status == "applied":
            return {**previous, "status": "duplicate", "original_status": "applied"}
        return previous

    try:
        _gate(ctx, op)
        payload_model, handler = HANDLERS[op.type]
        try:
            payload = payload_model.model_validate(op.payload)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise OpReject(E_PAYLOAD, f"{where}: {first['msg']}") from None
        async with db.begin_nested():
            outcome = await handler(ctx, ReplaySession(db), payload, op)
    except OpReject as rejection:
        return await _finish_rejected(ctx, op, payload_hash, rejection)
    except HTTPException as exc:
        return await _finish_rejected(ctx, op, payload_hash, _map_http(exc))
    except IntegrityError as exc:
        reason = str(getattr(exc.orig, "args", [""])[0])[:200]
        return await _finish_rejected(
            ctx, op, payload_hash,
            OpReject(E_ID_CONFLICT, f"Conflicts with existing data ({reason})", status="conflict"),
        )

    flags = list(outcome.flags)
    if ctx.lapsed and FLAG_SUBSCRIPTION not in flags:
        flags.append(FLAG_SUBSCRIPTION)
    status = "duplicate" if outcome.already_applied else "applied"
    result = _result(
        op.op_id, status, server_id=outcome.server_id, official_number=outcome.official_number,
        flags=flags, original_status="applied" if outcome.already_applied else None,
    )
    stored_result = {**result, "status": "applied", "original_status": None}
    await _store(ctx, op, payload_hash, "applied", stored_result)
    if not outcome.already_applied:
        await log_audit(
            db,
            business_id=ctx.business_id,
            user_id=ctx.user_id,
            branch_id=outcome.branch_id,
            action="sync",
            table_name=outcome.table,
            record_id=outcome.server_id or op.op_id,
            new_values={
                "event": "sync_op_applied",
                "op_type": op.type,
                "op_id": str(op.op_id),
                "batch_id": ctx.batch_id,
                "occurred_at": op.occurred_at.isoformat(),
                "official_number": outcome.official_number,
                "flags": flags,
            },
            device_id=ctx.device_id,
        )
    await db.commit()
    return result


async def _finish_rejected(
    ctx: Ctx, op: OpEnvelope, payload_hash: str, rejection: OpReject
) -> dict[str, Any]:
    result = _result(
        op.op_id,
        rejection.status,
        error=_error(rejection.code, rejection.message, rejection.retryable),
    )
    if not rejection.retryable:
        # Terminal: remember it so a retry gets the same answer without re-evaluating.
        await _store(ctx, op, payload_hash, rejection.status, result)
    await ctx.db.commit()
    return result


# ── batch ────────────────────────────────────────────────────────────────────


def _parse_op(raw: Any) -> tuple[OpEnvelope | None, dict[str, Any] | None]:
    """Return (op, None) or (None, rejected-result) for a malformed op."""
    op_id: UUID | None = None
    if isinstance(raw, dict):
        try:
            op_id = UUID(str(raw.get("op_id")))
        except ValueError:
            op_id = None
    if not isinstance(raw, dict):
        return None, _result(None, "rejected", error=_error(E_MALFORMED, "Op must be an object"))
    try:
        op = OpEnvelope.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return None, _result(op_id, "rejected", error=_error(E_MALFORMED, f"{where}: {first['msg']}"))
    if op.type not in HANDLERS:
        message = (
            "Held orders are not synced"
            if op.type.startswith(("held", "order.hold"))
            else f"Unsupported op type {op.type!r}"
        )
        return None, _result(op.op_id, "rejected", error=_error(E_UNSUPPORTED, message))
    if len(json.dumps(op.payload, default=str).encode()) > settings.sync_max_payload_bytes:
        return None, _result(
            op.op_id, "rejected",
            error=_error(E_TOO_LARGE, f"payload exceeds {settings.sync_max_payload_bytes} bytes"),
        )
    return op, None


async def _dependency_error(ctx: Ctx, op: OpEnvelope, batch: dict[UUID, dict]) -> dict | None:
    for dep in op.depends_on:
        outcome = batch.get(dep)
        if outcome is None:
            row = (
                await ctx.db.execute(
                    select(SyncOp.status).where(
                        SyncOp.business_id == ctx.business_id, SyncOp.op_id == dep
                    )
                )
            ).scalar_one_or_none()
            await ctx.db.commit()
            if row is None:
                return _error(E_DEP_MISSING, f"Dependency {dep} has not been synced", True)
            if row != "applied":
                return _error(E_DEP_FAILED, f"Dependency {dep} was {row}")
            continue
        if outcome["status"] in ("applied", "duplicate"):
            continue
        err = outcome.get("error") or {}
        return _error(E_DEP_FAILED, f"Dependency {dep} failed: {err.get('code')}", bool(err.get("retryable")))
    return None


async def process_push(
    db: AsyncSession,
    user: User,
    request: PushRequest,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    business = user.business
    ctx = Ctx(
        db=db,
        business_id=user.business_id,
        user_id=user.id,
        permissions=frozenset(_user_permission_keys(user)),
        branches=allowed_branch_ids(user),
        business_state=sub.compute_state(business, now),
        device_id=request.device_id,
        batch_id=request.batch_id,
        now=now,
    )

    results: list[dict[str, Any] | None] = [None] * len(request.ops)
    ops: dict[int, OpEnvelope] = {}
    for index, raw in enumerate(request.ops):
        op, rejected = _parse_op(raw)
        if op is None:
            results[index] = rejected
        else:
            ops[index] = op

    # Same op_id twice in one batch: the first wins, the second is a duplicate of it.
    first_index: dict[UUID, int] = {}
    for index in list(ops):
        op = ops[index]
        if op.op_id in first_index:
            del ops[index]
        else:
            first_index[op.op_id] = index
    duplicates = [
        (index, parse[0].op_id)
        for index, raw in enumerate(request.ops)
        if results[index] is None and index not in ops
        for parse in [_parse_op(raw)]
        if parse[0] is not None
    ]

    by_op_id: dict[UUID, dict] = {}
    finished: set[UUID] = set()
    remaining = sorted(ops)
    in_batch = {op.op_id for op in ops.values()}
    while remaining:
        pick = next(
            (
                i
                for i in remaining
                if all(d in finished for d in ops[i].depends_on if d in in_batch and d != ops[i].op_id)
            ),
            None,
        )
        if pick is None:  # dependency cycle
            pick = remaining[0]
            result = _result(
                ops[pick].op_id, "rejected", error=_error(E_DEP_CYCLE, "Circular depends_on")
            )
        else:
            op = ops[pick]
            error = await _dependency_error(ctx, op, by_op_id)
            result = (
                _result(op.op_id, "rejected", error=error)
                if error
                else await _run_op(ctx, op)
            )
        results[pick] = result
        by_op_id[ops[pick].op_id] = result
        finished.add(ops[pick].op_id)
        remaining.remove(pick)

    for index, op_id in duplicates:
        original = by_op_id[op_id]
        if original["status"] == "applied":
            results[index] = {**original, "status": "duplicate", "original_status": "applied"}
        else:
            results[index] = original
    return [r for r in results if r is not None]
