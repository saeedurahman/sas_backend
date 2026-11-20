"""Schemas for ``GET /products/lookup`` and ``POST /products/quick-add``.

The quick-add line fields are deliberately loose (``Decimal | str``, ``UUID | str``, ...): a bad value in
one line must produce an error for THAT line (``lines[].error``), never a ``422`` for the whole request.
Only the structure (``mode``, ``lines`` being a list, ``client_line_id``) is validated up front.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema

# ── lookup ───────────────────────────────────────────────────────────────────


class LookupBarcodeInfo(BaseSchema):
    input: str
    normalized: str  # canonical form: digits only, UPC-A 12 -> EAN-13 with a leading 0
    valid: bool  # GS1 check digit is correct (unknown formats are never rejected, just valid=false)
    format: str  # EAN8 | UPCA | EAN13 | GTIN14 | UNKNOWN
    matched_barcode: str | None = None  # the stored spelling that matched


class LookupUnit(BaseSchema):
    id: UUID | None = None
    name: str | None = None
    symbol: str | None = None


class LookupProduct(BaseSchema):
    id: UUID
    name: str
    variation_id: UUID | None = None
    unit: LookupUnit | None = None
    sale_price: Decimal | None = None  # effective default price list (null = no price yet)
    cost_price: Decimal | None = None  # last known cost (null = never had one)
    stock_on_hand: Decimal  # the requested branch (all your branches when none is given)
    track_expiry: bool = False
    image_url: str | None = None


class CatalogSuggestion(BaseSchema):
    """Extension point for the future Master Catalogue / Open Food Facts lookup.

    ``suggestion`` is always ``null`` today. When a catalogue source is wired in, a barcode that is
    not one of the business's own products returns this object so the client can prefill the form.
    """

    source: str  # e.g. "master_catalogue" | "openfoodfacts"
    name: str | None = None
    brand: str | None = None
    unit_hint: str | None = None
    pack_size: str | None = None
    category_hint: str | None = None
    image_url: str | None = None
    mrp: Decimal | None = None


class ProductLookupResponse(BaseSchema):
    found: bool
    barcode: LookupBarcodeInfo
    product: LookupProduct | None = None
    suggestion: CatalogSuggestion | None = None


# ── quick add ────────────────────────────────────────────────────────────────


class QuickAddLine(BaseSchema):
    client_line_id: str = Field(min_length=1, max_length=100)
    barcode: str | None = None
    name: str | None = None
    brand: str | None = None
    category_id: UUID | str | None = None
    unit_id: UUID | str | None = None
    pack_size: str | None = None
    qty: Decimal | str | None = None
    cost_price: Decimal | str | None = None
    sale_price: Decimal | str | None = None
    mrp: Decimal | str | None = None
    tax_rate_id: UUID | str | None = None
    is_tax_exempt: bool = False
    track_expiry: bool | None = None
    batch_no: str | None = None
    expiry_date: date | str | None = None
    image_url: str | None = None
    update_existing_prices: bool = False

    @field_validator("barcode", mode="before")
    @classmethod
    def _barcode_to_text(cls, value: Any) -> Any:
        # A JSON number loses leading zeros; keep it usable instead of failing the request.
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


class QuickAddRequest(BaseSchema):
    mode: Literal["opening_stock", "purchase"]
    supplier_id: UUID | None = None
    branch_id: UUID | None = None
    lines: list[QuickAddLine] = Field(min_length=1)


class QuickAddLineError(BaseSchema):
    code: str
    message: str


class QuickAddLineResult(BaseSchema):
    client_line_id: str
    status: Literal["created", "updated", "error"]
    product_id: UUID | None = None
    variation_id: UUID | None = None
    error: QuickAddLineError | None = None
    note: str | None = None  # e.g. "duplicate_barcode_merged"


class QuickAddResponse(BaseSchema):
    created: int
    updated: int
    failed: int
    purchase_id: UUID | None = None  # the purchase order (supplier purchase mode)
    receipt_id: UUID | None = None  # its goods receipt (GRN)
    adjustment_id: UUID | None = None  # the opening_balance adjustment (opening-stock mode)
    quick_add_id: UUID  # audit reference of this request
    lines: list[QuickAddLineResult]
