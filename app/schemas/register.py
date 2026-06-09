from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from app.models.enums import ReferenceTypeEnum, RegisterTxTypeEnum
from app.schemas.base import AuditSchema, BaseSchema


class CreateCashRegisterRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    branch_id: UUID
    device_identifier: str | None = None


class UpdateCashRegisterRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    device_identifier: str | None = None
    is_active: bool | None = None


class OpenShiftRequest(BaseSchema):
    cash_register_id: UUID
    opening_float: Decimal
    notes: str | None = None

    @field_validator("opening_float")
    @classmethod
    def validate_opening_float(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("opening_float must be >= 0")
        return value


class CloseShiftRequest(BaseSchema):
    actual_cash: Decimal
    notes: str | None = None

    @field_validator("actual_cash")
    @classmethod
    def validate_actual_cash(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("actual_cash must be >= 0")
        return value


class CreateRegisterTransactionRequest(BaseSchema):
    tx_type: RegisterTxTypeEnum
    amount: Decimal
    notes: str | None = None
    reference_type: ReferenceTypeEnum | None = None
    reference_id: UUID | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be greater than 0")
        return value


class CashRegisterResponse(AuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    name: str
    device_identifier: str | None = None
    is_active: bool


class RegisterTransactionResponse(BaseSchema):
    id: UUID
    business_id: UUID
    register_shift_id: UUID
    tx_type: str
    payment_method: str
    amount: Decimal
    reference_type: str | None = None
    reference_id: UUID | None = None
    notes: str | None = None
    transacted_at: datetime
    created_at: datetime


class RegisterShiftResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    cash_register_id: UUID
    opened_by: UUID
    closed_by: UUID | None = None
    status: str
    opening_float: Decimal
    expected_cash: Decimal | None = None
    actual_cash: Decimal | None = None
    cash_difference: Decimal | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    transactions: list[RegisterTransactionResponse] = Field(default_factory=list)


class ShiftSummaryResponse(BaseSchema):
    shift_id: UUID
    status: str
    opening_float: Decimal
    total_cash_sales: Decimal
    total_card_sales: Decimal
    total_wallet_sales: Decimal
    total_other_sales: Decimal
    total_returns: Decimal
    total_cash_in: Decimal
    total_cash_out: Decimal
    total_expenses: Decimal
    expected_cash: Decimal
    actual_cash: Decimal | None = None
    cash_difference: Decimal | None = None
