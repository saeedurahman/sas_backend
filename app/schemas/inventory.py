from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import EmailStr, Field, field_validator, model_validator

from app.models.enums import (
    AdjustmentReasonEnum,
    PurchaseOrderStatusEnum,
)
from app.schemas.base import BaseSchema


class InventoryAuditSchema(BaseSchema):
    """Audit timestamps only — sync and deleted_at excluded from API responses."""

    created_at: datetime
    updated_at: datetime


# ── Request schemas (child before parent) ────────────────────────────────────


class CreatePurchaseLineRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    ordered_qty: Decimal
    cost_per_unit: Decimal
    tax_rate: Decimal = Decimal("0")
    batch_number: str | None = None
    expiry_date: date | None = None

    @field_validator("ordered_qty")
    @classmethod
    def validate_ordered_qty(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("ordered_qty must be greater than 0")
        return value

    @field_validator("cost_per_unit")
    @classmethod
    def validate_cost(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("cost_per_unit cannot be negative")
        return value

    @field_validator("tax_rate")
    @classmethod
    def validate_tax_rate(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 100:
            raise ValueError("tax_rate must be between 0 and 100")
        return value


class CreatePurchaseOrderRequest(BaseSchema):
    supplier_id: UUID
    branch_id: UUID
    expected_at: datetime | None = None
    notes: str | None = None
    lines: list[CreatePurchaseLineRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lines(self) -> "CreatePurchaseOrderRequest":
        if len(self.lines) < 1:
            raise ValueError("At least one purchase line is required")
        return self


class UpdatePurchaseOrderRequest(BaseSchema):
    expected_at: datetime | None = None
    notes: str | None = None
    status: PurchaseOrderStatusEnum | None = None


class CreateReceiptLineRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    purchase_line_id: UUID | None = None
    qty_received: Decimal
    cost_per_unit: Decimal
    batch_number: str | None = None
    expiry_date: date | None = None

    @field_validator("qty_received")
    @classmethod
    def validate_qty_received(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("qty_received must be greater than 0")
        return value

    @field_validator("cost_per_unit")
    @classmethod
    def validate_cost(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("cost_per_unit cannot be negative")
        return value


class CreatePurchaseReceiptRequest(BaseSchema):
    branch_id: UUID
    supplier_id: UUID
    purchase_order_id: UUID | None = None
    supplier_invoice_no: str | None = None
    notes: str | None = None
    received_at: datetime | None = None
    lines: list[CreateReceiptLineRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lines(self) -> "CreatePurchaseReceiptRequest":
        if len(self.lines) < 1:
            raise ValueError("At least one receipt line is required")
        return self


class CreateAdjustmentLineRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    qty_delta: Decimal
    cost_per_unit: Decimal
    notes: str | None = None

    @field_validator("qty_delta")
    @classmethod
    def validate_qty_delta(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("qty_delta cannot be zero")
        return value

    @field_validator("cost_per_unit")
    @classmethod
    def validate_cost(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("cost_per_unit cannot be negative")
        return value


class CreateStockAdjustmentRequest(BaseSchema):
    branch_id: UUID
    reason: AdjustmentReasonEnum
    notes: str | None = None
    adjusted_at: datetime | None = None
    lines: list[CreateAdjustmentLineRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lines(self) -> "CreateStockAdjustmentRequest":
        if len(self.lines) < 1:
            raise ValueError("At least one adjustment line is required")
        return self


class CreateTransferLineRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    qty: Decimal
    cost_per_unit: Decimal

    @field_validator("qty")
    @classmethod
    def validate_qty(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("qty must be greater than 0")
        return value

    @field_validator("cost_per_unit")
    @classmethod
    def validate_cost(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("cost_per_unit cannot be negative")
        return value


class CreateStockTransferRequest(BaseSchema):
    source_branch_id: UUID
    dest_branch_id: UUID
    notes: str | None = None
    lines: list[CreateTransferLineRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_transfer(self) -> "CreateStockTransferRequest":
        if self.source_branch_id == self.dest_branch_id:
            raise ValueError("source_branch_id and dest_branch_id must be different")
        if len(self.lines) < 1:
            raise ValueError("At least one transfer line is required")
        return self


class CreateWasteLineRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    qty: Decimal
    cost_per_unit: Decimal
    batch_number: str | None = None
    expiry_date: date | None = None

    @field_validator("qty")
    @classmethod
    def validate_qty(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("qty must be greater than 0")
        return value

    @field_validator("cost_per_unit")
    @classmethod
    def validate_cost(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("cost_per_unit cannot be negative")
        return value


class CreateWasteEntryRequest(BaseSchema):
    branch_id: UUID
    reason: AdjustmentReasonEnum
    notes: str | None = None
    wasted_at: datetime | None = None
    lines: list[CreateWasteLineRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lines(self) -> "CreateWasteEntryRequest":
        if len(self.lines) < 1:
            raise ValueError("At least one waste line is required")
        return self


class CreateSupplierRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=255)
    contact_person: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    tax_id: str | None = None
    address_line1: str | None = None
    city: str | None = None
    payment_terms_days: int = 30

    @field_validator("payment_terms_days")
    @classmethod
    def validate_payment_terms(cls, value: int) -> int:
        if value < 0:
            raise ValueError("payment_terms_days must be >= 0")
        return value


class UpdateSupplierRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    contact_person: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    is_active: bool | None = None
    payment_terms_days: int | None = None

    @field_validator("payment_terms_days")
    @classmethod
    def validate_payment_terms(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("payment_terms_days must be >= 0")
        return value


# ── Response schemas (child before parent) ───────────────────────────────────


class SupplierResponse(InventoryAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    contact_person: str | None
    email: str | None
    phone: str | None
    tax_id: str | None
    address_line1: str | None
    city: str | None
    payment_terms_days: int
    is_active: bool


class PurchaseLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    purchase_order_id: UUID
    product_id: UUID
    variation_id: UUID | None
    ordered_qty: Decimal
    received_qty: Decimal
    qty_remaining: Decimal
    cost_per_unit: Decimal
    tax_rate: Decimal
    batch_number: str | None
    expiry_date: date | None


class PurchaseOrderResponse(InventoryAuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    supplier_id: UUID
    po_number: str
    status: str
    ordered_at: datetime | None
    expected_at: datetime | None
    notes: str | None
    supplier: SupplierResponse | None = None
    lines: list[PurchaseLineResponse] = Field(default_factory=list)


class PurchaseReceiptLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    purchase_receipt_id: UUID
    purchase_line_id: UUID | None
    product_id: UUID
    variation_id: UUID | None
    qty_received: Decimal
    cost_per_unit: Decimal
    batch_number: str | None
    expiry_date: date | None


class PurchaseReceiptResponse(InventoryAuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    supplier_id: UUID
    purchase_order_id: UUID | None
    receipt_number: str
    received_at: datetime
    supplier_invoice_no: str | None
    notes: str | None
    supplier: SupplierResponse | None = None
    lines: list[PurchaseReceiptLineResponse] = Field(default_factory=list)


class StockMovementResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    product_id: UUID
    variation_id: UUID | None
    movement_type: str
    qty: Decimal
    cost_per_unit: Decimal
    reference_type: str
    reference_id: UUID
    purchase_line_id: UUID | None
    batch_number: str | None
    expiry_date: date | None
    notes: str | None
    movement_at: datetime
    created_at: datetime


class StockAdjustmentLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    stock_adjustment_id: UUID
    product_id: UUID
    variation_id: UUID | None
    qty_delta: Decimal
    cost_per_unit: Decimal
    notes: str | None


class StockAdjustmentResponse(InventoryAuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    adjustment_number: str
    reason: str
    adjusted_at: datetime
    notes: str | None
    lines: list[StockAdjustmentLineResponse] = Field(default_factory=list)


class StockTransferLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    stock_transfer_id: UUID
    product_id: UUID
    variation_id: UUID | None
    qty: Decimal
    cost_per_unit: Decimal
    qty_received: Decimal


class StockTransferResponse(InventoryAuditSchema):
    id: UUID
    business_id: UUID
    transfer_number: str
    source_branch_id: UUID
    dest_branch_id: UUID
    status: str
    transferred_at: datetime | None
    received_at: datetime | None
    notes: str | None
    lines: list[StockTransferLineResponse] = Field(default_factory=list)


class WasteEntryLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    waste_entry_id: UUID
    product_id: UUID
    variation_id: UUID | None
    qty: Decimal
    cost_per_unit: Decimal
    batch_number: str | None
    expiry_date: date | None


class WasteEntryResponse(InventoryAuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    waste_number: str
    wasted_at: datetime
    reason: str
    notes: str | None
    lines: list[WasteEntryLineResponse] = Field(default_factory=list)


class StockBalanceResponse(BaseSchema):
    product_id: UUID
    variation_id: UUID | None
    branch_id: UUID
    current_qty: Decimal
    last_movement_at: datetime | None


class PaginatedStockMovementResponse(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[StockMovementResponse]
