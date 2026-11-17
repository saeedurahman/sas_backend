from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema


class BusinessInfoForInvoice(BaseSchema):
    name: str
    logo_url: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    tax_id: str | None = None
    city: str | None = None
    currency: str


class CustomerInfoForInvoice(BaseSchema):
    name: str
    phone: str | None = None
    address: str | None = None
    tax_id: str | None = None


class InvoiceLineBatch(BaseSchema):
    batch_no: str | None = None
    expiry_date: date | None = None
    qty: Decimal


class InvoiceLineItem(BaseSchema):
    line_order: int
    product_name: str
    product_name_secondary: str | None = None
    # Only set when the variation has a real name (never "Default" / empty).
    variation_name: str | None = None
    sku: str | None = None
    barcode: str | None = None
    unit_allows_decimal: bool = False
    batches: list[InvoiceLineBatch] = Field(default_factory=list)
    qty: Decimal
    unit: str | None = None
    unit_price: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    tax_rate: Decimal
    net_amount: Decimal = Decimal("0")
    tax_amount: Decimal
    # Gross of the line (net + tax); equals qty x price - discount in inclusive mode.
    line_total: Decimal


class InvoiceTaxBreakdownItem(BaseSchema):
    tax_label: str
    tax_rate: Decimal
    net_amount: Decimal
    tax_amount: Decimal


class InvoicePaymentItem(BaseSchema):
    payment_method: str
    amount: Decimal
    reference_no: str | None = None
    paid_at: datetime


class InvoiceSummary(BaseSchema):
    # Tax/price settings the sale was made with.
    tax_inclusive: bool = False
    tax_label: str = "GST"
    tax_rounding_mode: str = "none"
    # Display flag from the business settings (tax on + show breakdown + tax > 0).
    show_tax_breakdown: bool = False
    subtotal: Decimal
    total_discount: Decimal
    net_amount: Decimal = Decimal("0")
    total_tax: Decimal
    exact_total: Decimal = Decimal("0")
    rounding_amount: Decimal = Decimal("0")
    # Rounded grand total = exact_total + rounding_amount.
    total_amount: Decimal
    tax_breakdown: list[InvoiceTaxBreakdownItem] = Field(default_factory=list)
    total_paid: Decimal
    balance_due: Decimal
    change_given: Decimal


class InvoiceData(BaseSchema):
    sale_id: UUID
    sale_number: str
    client_receipt_no: str | None = None
    sale_type: str
    status: str
    sold_at: datetime
    branch_id: UUID
    branch_name: str
    branch_address: str | None = None
    branch_phone: str | None = None
    cashier_name: str
    business: BusinessInfoForInvoice
    customer: CustomerInfoForInvoice | None = None
    lines: list[InvoiceLineItem]
    payments: list[InvoicePaymentItem]
    summary: InvoiceSummary
    receipt_footer: str | None = None
    printed_at: datetime
    register_name: str | None = None
    # Customer ledger, shown as "owed" amounts (positive = customer owes the shop).
    customer_previous_balance: Decimal | None = None
    customer_balance_after: Decimal | None = None
    # The sale exists on the server, so a public receipt link can point at it.
    synced: bool = True


class ThermalLineItem(BaseSchema):
    name: str
    qty: Decimal
    unit_price: Decimal
    line_total: Decimal


class ThermalReceiptData(BaseSchema):
    sale_number: str
    client_receipt_no: str | None = None
    sold_at: datetime
    branch_name: str
    cashier_name: str
    business_name: str
    lines: list[ThermalLineItem]
    summary: InvoiceSummary
    payments: list[InvoicePaymentItem]
    footer: str | None = None
    paper_size: str
    # The same receipt rendered with the business's "print" layout (see receipt_render).
    receipt: dict | None = None


class ExportSalesRequest(BaseSchema):
    date_from: date
    date_to: date
    branch_id: UUID | None = None
    format: str = "csv"

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        if value not in ("csv", "excel"):
            raise ValueError("format must be 'csv' or 'excel'")
        return value
