from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import EmailStr, Field, field_validator, model_validator

from app.models.enums import (
    DiscountTypeEnum,
    PaymentMethodEnum,
    SaleTypeEnum,
)
from app.schemas.base import BaseSchema


class SalesAuditSchema(BaseSchema):
    """Audit timestamps only — sync and deleted_at excluded from API responses."""

    created_at: datetime
    updated_at: datetime


# ── Request schemas (child before parent) ────────────────────────────────────


class CreateCustomerRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=255)
    email: EmailStr | None = None
    phone: str | None = None
    tax_id: str | None = None
    address_line1: str | None = None
    city: str | None = None
    credit_limit: Decimal = Decimal("0")

    @field_validator("credit_limit")
    @classmethod
    def validate_credit_limit(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("credit_limit must be >= 0")
        return value


class UpdateCustomerRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    email: EmailStr | None = None
    phone: str | None = None
    credit_limit: Decimal | None = None
    is_active: bool | None = None

    @field_validator("credit_limit")
    @classmethod
    def validate_credit_limit(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("credit_limit must be >= 0")
        return value


class CreateTaxRateRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    rate: Decimal
    is_compound: bool = False

    @field_validator("rate")
    @classmethod
    def validate_rate(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 100:
            raise ValueError("rate must be between 0 and 100")
        return value


class UpdateTaxRateRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    rate: Decimal | None = None
    is_active: bool | None = None

    @field_validator("rate")
    @classmethod
    def validate_rate(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (value < 0 or value > 100):
            raise ValueError("rate must be between 0 and 100")
        return value


class CreateDiscountSchemeRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    discount_type: DiscountTypeEnum
    discount_value: Decimal
    min_purchase_amount: Decimal | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    applies_to_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("discount_value")
    @classmethod
    def validate_discount_value(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("discount_value must be greater than 0")
        return value

    @model_validator(mode="after")
    def validate_discount_scheme(self) -> "CreateDiscountSchemeRequest":
        if (
            self.discount_type == DiscountTypeEnum.percentage
            and self.discount_value > 100
        ):
            raise ValueError("percentage discount_value must be <= 100")
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_to <= self.valid_from:
                raise ValueError("valid_to must be after valid_from")
        return self


class CreateSaleLineRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    qty: Decimal
    unit_price: Decimal
    discount_pct: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")
    notes: str | None = None
    line_order: int = 0

    @field_validator("qty")
    @classmethod
    def validate_qty(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("qty must be greater than 0")
        return value

    @field_validator("unit_price")
    @classmethod
    def validate_unit_price(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("unit_price cannot be negative")
        return value

    @field_validator("discount_pct")
    @classmethod
    def validate_discount_pct(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 100:
            raise ValueError("discount_pct must be between 0 and 100")
        return value

    @field_validator("discount_amount")
    @classmethod
    def validate_discount_amount(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("discount_amount cannot be negative")
        return value

    @field_validator("tax_rate")
    @classmethod
    def validate_tax_rate(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("tax_rate cannot be negative")
        return value


class CreatePaymentRequest(BaseSchema):
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


class CreateSaleRequest(BaseSchema):
    branch_id: UUID
    customer_id: UUID | None = None
    sale_type: SaleTypeEnum = SaleTypeEnum.pos
    price_list_id: UUID | None = None
    discount_scheme_id: UUID | None = None
    notes: str | None = None
    sold_at: datetime | None = None
    lines: list[CreateSaleLineRequest]
    payments: list[CreatePaymentRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lines(self) -> "CreateSaleRequest":
        if len(self.lines) < 1:
            raise ValueError("At least one line required")
        return self


class CreateReturnLineRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    sale_line_id: UUID | None = None
    qty: Decimal
    unit_price: Decimal
    tax_amount: Decimal = Decimal("0")

    @field_validator("qty")
    @classmethod
    def validate_qty(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("qty must be greater than 0")
        return value

    @field_validator("unit_price")
    @classmethod
    def validate_unit_price(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("unit_price cannot be negative")
        return value

    @field_validator("tax_amount")
    @classmethod
    def validate_tax_amount(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("tax_amount cannot be negative")
        return value


class CreateRefundPaymentRequest(BaseSchema):
    payment_method: PaymentMethodEnum
    amount: Decimal

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be greater than 0")
        return value


class CreateSaleReturnRequest(BaseSchema):
    branch_id: UUID
    sale_id: UUID | None = None
    customer_id: UUID | None = None
    reason: str | None = None
    returned_at: datetime | None = None
    lines: list[CreateReturnLineRequest]
    refund_payments: list[CreateRefundPaymentRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lines(self) -> "CreateSaleReturnRequest":
        if len(self.lines) < 1:
            raise ValueError("At least one return line required")
        return self


# ── Response schemas (child before parent) ───────────────────────────────────


class CustomerResponse(SalesAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    email: str | None
    phone: str | None
    tax_id: str | None
    address_line1: str | None
    city: str | None
    credit_limit: Decimal
    is_active: bool


class CustomerLedgerResponse(BaseSchema):
    id: UUID
    business_id: UUID
    customer_id: UUID
    entry_type: str
    amount: Decimal
    reference_type: str | None
    reference_id: UUID | None
    entry_at: datetime
    notes: str | None
    created_at: datetime


class TaxRateResponse(SalesAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    rate: Decimal
    is_compound: bool
    is_active: bool


class DiscountSchemeResponse(SalesAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    discount_type: str
    discount_value: Decimal
    min_purchase_amount: Decimal | None
    valid_from: datetime | None
    valid_to: datetime | None
    applies_to_json: dict[str, Any]
    is_active: bool


class SalePaymentResponse(BaseSchema):
    id: UUID
    business_id: UUID
    sale_id: UUID
    payment_method: str
    amount: Decimal
    status: str
    reference_no: str | None
    paid_at: datetime
    created_at: datetime


class SaleLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    sale_id: UUID
    product_id: UUID
    variation_id: UUID | None
    qty: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    cost_per_unit: Decimal
    notes: str | None
    line_order: int


class SaleReturnPaymentResponse(BaseSchema):
    id: UUID
    business_id: UUID
    sale_return_id: UUID
    payment_method: str
    amount: Decimal
    status: str
    refunded_at: datetime
    created_at: datetime


class SaleReturnLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    sale_return_id: UUID
    sale_line_id: UUID | None
    product_id: UUID
    variation_id: UUID | None
    qty: Decimal
    unit_price: Decimal
    tax_amount: Decimal


class SaleReturnResponse(SalesAuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    sale_id: UUID | None
    customer_id: UUID | None
    return_number: str
    returned_at: datetime
    reason: str | None
    lines: list[SaleReturnLineResponse] = Field(default_factory=list)
    payments: list[SaleReturnPaymentResponse] = Field(default_factory=list)


class SaleResponse(SalesAuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    customer_id: UUID | None
    sale_number: str
    sale_type: str
    status: str
    sold_at: datetime
    notes: str | None
    lines: list[SaleLineResponse] = Field(default_factory=list)
    payments: list[SalePaymentResponse] = Field(default_factory=list)


class SaleListResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    customer_id: UUID | None
    sale_number: str
    sale_type: str
    status: str
    sold_at: datetime


class PaginatedSaleResponse(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[SaleListResponse]
