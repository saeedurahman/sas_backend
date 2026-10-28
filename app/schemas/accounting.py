from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.base import AuditSchema, BaseSchema


class ChartOfAccountResponse(AuditSchema):
    id: UUID
    business_id: UUID
    parent_id: UUID | None = None
    account_code: str
    account_name: str
    account_type: str
    account_subtype: str | None = None
    is_system: bool
    is_active: bool
    description: str | None = None


class ChartOfAccountTreeNode(ChartOfAccountResponse):
    children: list["ChartOfAccountTreeNode"] = Field(default_factory=list)


class CreateChartOfAccountRequest(BaseSchema):
    account_code: str = Field(min_length=1, max_length=20)
    account_name: str = Field(min_length=1, max_length=255)
    account_type: str
    account_subtype: str | None = None
    parent_id: UUID | None = None
    description: str | None = None
    is_active: bool = True

    @field_validator("account_code", "account_name")
    @classmethod
    def strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class UpdateChartOfAccountRequest(BaseSchema):
    account_code: str | None = Field(default=None, min_length=1, max_length=20)
    account_name: str | None = Field(default=None, min_length=1, max_length=255)
    account_type: str | None = None
    account_subtype: str | None = None
    parent_id: UUID | None = None
    description: str | None = None
    is_active: bool | None = None

    @field_validator("account_code", "account_name")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


ChartOfAccountTreeNode.model_rebuild()


class JournalLineInput(BaseSchema):
    account_id: UUID
    debit_amount: Decimal = Field(default=Decimal("0"), ge=0)
    credit_amount: Decimal = Field(default=Decimal("0"), ge=0)
    description: str | None = None
    line_order: int = 0

    @model_validator(mode="after")
    def validate_one_side(self) -> "JournalLineInput":
        has_debit = self.debit_amount > 0
        has_credit = self.credit_amount > 0
        if has_debit == has_credit:
            raise ValueError("Each line must have exactly one of debit or credit greater than zero")
        return self


class JournalLineResponse(BaseSchema):
    id: UUID
    account_id: UUID
    account_code: str
    account_name: str
    debit_amount: Decimal
    credit_amount: Decimal
    description: str | None = None
    line_order: int


class CreateJournalEntryRequest(BaseSchema):
    entry_date: date | None = None
    description: str | None = None
    branch_id: UUID | None = None
    lines: list[JournalLineInput] = Field(min_length=1)


class UpdateJournalEntryRequest(BaseSchema):
    entry_date: date | None = None
    description: str | None = None
    branch_id: UUID | None = None
    lines: list[JournalLineInput] | None = None


class JournalEntryListResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID | None = None
    entry_number: str
    status: str
    entry_date: date
    description: str | None = None
    reference_type: str | None = None
    posted_at: datetime | None = None
    total_debit: Decimal
    total_credit: Decimal
    created_at: datetime


class JournalEntryResponse(AuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID | None = None
    entry_number: str
    status: str
    entry_date: date
    description: str | None = None
    reference_type: str | None = None
    reference_id: UUID | None = None
    posted_at: datetime | None = None
    lines: list[JournalLineResponse] = Field(default_factory=list)
    total_debit: Decimal
    total_credit: Decimal
