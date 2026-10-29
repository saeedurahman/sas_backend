"""Request/response shapes for the offline sync API (see README "Offline sync")."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.models.enums import PaymentMethodEnum, SaleTypeEnum
from app.schemas.base import BaseSchema

# ── pull / snapshot ──────────────────────────────────────────────────────────


class SyncChange(BaseSchema):
    entity: str
    id: UUID
    op: str  # "upsert" | "delete"
    data: dict[str, Any] | None = None  # null for deletes
    seq: int


class PullResponse(BaseSchema):
    changes: list[SyncChange]
    next_cursor: str | None
    has_more: bool
    server_time: datetime
    full_resync_required: bool = False
    full_resync_reason: str | None = None
    permissions_version: str
    entities: list[str]  # entity types this user receives
    restricted: bool = False  # blocked subscription: only the expired-screen data is served


class SnapshotResponse(BaseSchema):
    changes: list[SyncChange]
    next_page_token: str | None
    has_more: bool
    cursor: str  # start pulling from here once the last page is applied
    server_time: datetime
    permissions_version: str
    entities: list[str]
    restricted: bool = False


# ── push ─────────────────────────────────────────────────────────────────────


class PushRequest(BaseSchema):
    device_id: str = Field(min_length=1, max_length=100)
    batch_id: str = Field(min_length=1, max_length=100)
    device_name: str | None = Field(default=None, max_length=150)
    app_version: str | None = Field(default=None, max_length=50)
    # Validated one by one so a malformed op is rejected without failing the batch.
    ops: list[dict[str, Any]] = Field(default_factory=list)


class OpError(BaseSchema):
    code: str
    message: str
    retryable: bool = False


class OpResult(BaseSchema):
    op_id: UUID | None = None
    status: str  # applied | duplicate | rejected | conflict
    server_id: UUID | None = None
    official_number: str | None = None
    flags: list[str] = Field(default_factory=list)
    error: OpError | None = None
    original_status: str | None = None  # set when status == "duplicate"


class PushResponse(BaseSchema):
    batch_id: str
    server_time: datetime
    results: list[OpResult]


class OpEnvelope(BaseSchema):
    op_id: UUID
    type: str
    client_created_at: datetime
    occurred_at: datetime
    payload: dict[str, Any]
    depends_on: list[UUID] = Field(default_factory=list)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        if value is None:
            return []
        return [value] if isinstance(value, (str, UUID)) else value

    @field_validator("client_created_at", "occurred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must include a UTC offset")
        return value


# ── op payloads ──────────────────────────────────────────────────────────────


class CustomerCreatePayload(BaseSchema):
    id: UUID
    name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    tax_id: str | None = Field(default=None, max_length=100)
    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    credit_limit: Decimal = Decimal("0")

    @field_validator("credit_limit")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("credit_limit must be >= 0")
        return value


class SaleLinePayload(BaseSchema):
    id: UUID | None = None  # client line id, so a later return can reference the line
    product_id: UUID
    variation_id: UUID | None = None
    qty: Decimal
    unit_price: Decimal
    discount_pct: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    notes: str | None = None
    line_order: int | None = None

    @model_validator(mode="after")
    def _check(self) -> "SaleLinePayload":
        if self.qty <= 0:
            raise ValueError("qty must be greater than 0")
        for name in ("unit_price", "discount_pct", "discount_amount", "tax_rate", "tax_amount"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        return self


class SalePaymentPayload(BaseSchema):
    payment_method: PaymentMethodEnum
    amount: Decimal  # applied to the sale (net of change)
    tendered_amount: Decimal | None = None
    change_given: Decimal = Decimal("0")
    reference_no: str | None = Field(default=None, max_length=100)
    paid_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> "SalePaymentPayload":
        if self.amount <= 0:
            raise ValueError("payment amount must be greater than 0")
        if self.change_given < 0:
            raise ValueError("change_given must be >= 0")
        return self


class SaleCreatePayload(BaseSchema):
    id: UUID
    client_receipt_no: str = Field(min_length=1, max_length=64)
    branch_id: UUID
    register_shift_id: UUID | None = None
    customer_id: UUID | None = None
    sale_type: SaleTypeEnum = SaleTypeEnum.pos
    price_list_id: UUID | None = None
    discount_scheme_id: UUID | None = None
    notes: str | None = None
    lines: list[SaleLinePayload] = Field(min_length=1)
    payments: list[SalePaymentPayload] = Field(default_factory=list)


class SaleVoidPayload(BaseSchema):
    sale_id: UUID
    reason: str | None = Field(default=None, max_length=500)


class CustomerPaymentPayload(BaseSchema):
    id: UUID | None = None  # becomes the ledger entry id
    customer_id: UUID
    amount: Decimal
    payment_method: PaymentMethodEnum = PaymentMethodEnum.cash
    register_shift_id: UUID | None = None
    reference_no: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be greater than 0")
        return value


class ExpensePaymentPayload(BaseSchema):
    payment_method: PaymentMethodEnum
    amount: Decimal
    reference_no: str | None = None
    paid_at: datetime | None = None


class ExpenseCreatePayload(BaseSchema):
    branch_id: UUID
    expense_category_id: UUID
    supplier_id: UUID | None = None
    description: str | None = None
    expense_date: date
    amount: Decimal
    tax_amount: Decimal = Decimal("0")
    payments: list[ExpensePaymentPayload] = Field(default_factory=list)


class ShiftOpenPayload(BaseSchema):
    id: UUID
    cash_register_id: UUID
    opening_float: Decimal = Decimal("0")
    notes: str | None = None

    @field_validator("opening_float")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("opening_float must be >= 0")
        return value


class ShiftClosePayload(BaseSchema):
    shift_id: UUID
    actual_cash: Decimal
    notes: str | None = None

    @field_validator("actual_cash")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("actual_cash must be >= 0")
        return value


class ReturnLinePayload(BaseSchema):
    sale_line_id: UUID | None = None
    product_id: UUID | None = None
    variation_id: UUID | None = None
    qty: Decimal
    unit_price: Decimal | None = None
    tax_amount: Decimal | None = None


class RefundPaymentPayload(BaseSchema):
    payment_method: PaymentMethodEnum
    amount: Decimal
    reference_no: str | None = None


class ReturnCreatePayload(BaseSchema):
    branch_id: UUID
    sale_id: UUID | None = None
    register_shift_id: UUID | None = None
    customer_id: UUID | None = None
    reason: str | None = None
    lines: list[ReturnLinePayload] = Field(min_length=1)
    refund_payments: list[RefundPaymentPayload] = Field(default_factory=list)


# ── devices / flags ──────────────────────────────────────────────────────────


class DeviceResponse(BaseSchema):
    device_id: str
    name: str | None = None
    app_version: str | None = None
    last_user_id: UUID | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None


class FlaggedSale(BaseSchema):
    id: UUID
    branch_id: UUID
    sale_number: str
    client_receipt_no: str | None = None
    device_id: str | None = None
    sold_at: datetime
    synced_at: datetime | None = None
    flags: list[str]
    status: str
    total_amount: Decimal
    cashier_name: str


class FlaggedSalesResponse(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[FlaggedSale]


class TimeResponse(BaseSchema):
    server_time: datetime
    epoch_ms: int
