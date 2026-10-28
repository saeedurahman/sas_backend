from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from app.models.enums import PaymentMethodEnum
from app.schemas.base import AuditSchema, BaseSchema


class CreateExpenseCategoryRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    parent_id: UUID | None = None
    is_active: bool = True


class UpdateExpenseCategoryRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    is_active: bool | None = None


class CreateExpensePaymentRequest(BaseSchema):
    payment_method: PaymentMethodEnum
    amount: Decimal
    reference_no: str | None = None
    paid_at: datetime | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be greater than 0")
        return value

    @field_validator("payment_method")
    @classmethod
    def reject_credit_payment_method(
        cls, value: PaymentMethodEnum
    ) -> PaymentMethodEnum:
        if value == PaymentMethodEnum.credit:
            raise ValueError(
                "credit is not a valid payment method for expense payments"
            )
        return value


class CreateExpenseRequest(BaseSchema):
    branch_id: UUID
    expense_category_id: UUID
    supplier_id: UUID | None = None
    description: str | None = None
    expense_date: date
    amount: Decimal
    tax_amount: Decimal = Decimal("0")
    payments: list[CreateExpensePaymentRequest] = Field(default_factory=list)

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be greater than 0")
        return value

    @field_validator("tax_amount")
    @classmethod
    def validate_tax_amount(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("tax_amount must be >= 0")
        return value


class UpdateExpenseRequest(BaseSchema):
    description: str | None = None
    expense_date: date | None = None
    amount: Decimal | None = None
    tax_amount: Decimal | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("amount must be greater than 0")
        return value

    @field_validator("tax_amount")
    @classmethod
    def validate_tax_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("tax_amount must be >= 0")
        return value


class ExpenseCategoryResponse(AuditSchema):
    id: UUID
    business_id: UUID
    name: str
    parent_id: UUID | None = None
    is_active: bool
    children: list["ExpenseCategoryResponse"] = Field(default_factory=list)


ExpenseCategoryResponse.model_rebuild()


class ExpensePaymentResponse(BaseSchema):
    id: UUID
    business_id: UUID
    expense_id: UUID
    payment_method: str
    amount: Decimal
    status: str
    reference_no: str | None = None
    paid_at: datetime
    created_at: datetime
    total_paid: Decimal | None = None
    remaining_balance: Decimal | None = None


class ExpenseResponse(AuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    expense_category_id: UUID
    supplier_id: UUID | None = None
    expense_number: str
    description: str | None = None
    expense_date: date
    amount: Decimal
    tax_amount: Decimal
    payments: list[ExpensePaymentResponse] = Field(default_factory=list)


class ExpenseListResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    expense_category_id: UUID
    supplier_id: UUID | None = None
    expense_number: str
    description: str | None = None
    expense_date: date
    amount: Decimal
    tax_amount: Decimal
    created_at: datetime


class PaginatedExpenseResponse(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[ExpenseListResponse]


class SupplierLedgerResponse(BaseSchema):
    id: UUID
    business_id: UUID
    supplier_id: UUID
    entry_type: str
    amount: Decimal
    reference_type: str | None = None
    reference_id: UUID | None = None
    entry_at: datetime
    notes: str | None = None
    created_at: datetime


class SupplierLedgerListResponse(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[SupplierLedgerResponse]


class SupplierBalanceResponse(BaseSchema):
    supplier_id: UUID
    balance: Decimal


class SupplierPaymentRequest(BaseSchema):
    amount: Decimal
    payment_method: PaymentMethodEnum
    reference_no: str | None = None
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be greater than 0")
        return value
