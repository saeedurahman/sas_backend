from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import field_validator

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


class InvoiceLineItem(BaseSchema):
    line_order: int
    product_name: str
    variation_name: str | None = None
    qty: Decimal
    unit: str | None = None
    unit_price: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_total: Decimal


class InvoicePaymentItem(BaseSchema):
    payment_method: str
    amount: Decimal
    reference_no: str | None = None
    paid_at: datetime


class InvoiceSummary(BaseSchema):
    subtotal: Decimal
    total_discount: Decimal
    total_tax: Decimal
    total_amount: Decimal
    total_paid: Decimal
    balance_due: Decimal
    change_given: Decimal


class InvoiceData(BaseSchema):
    sale_id: UUID
    sale_number: str
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


class ThermalLineItem(BaseSchema):
    name: str
    qty: Decimal
    unit_price: Decimal
    line_total: Decimal


class ThermalReceiptData(BaseSchema):
    sale_number: str
    sold_at: datetime
    branch_name: str
    cashier_name: str
    business_name: str
    lines: list[ThermalLineItem]
    summary: InvoiceSummary
    payments: list[InvoicePaymentItem]
    footer: str | None = None
    paper_size: str


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
