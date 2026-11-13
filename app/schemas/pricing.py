"""Request/response schemas for the price-list management API."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.models.enums import PriceListTypeEnum
from app.schemas.base import BaseSchema


def _check_window(valid_from: datetime | None, valid_to: datetime | None) -> None:
    if valid_from is not None and valid_to is not None and valid_to <= valid_from:
        raise ValueError("valid_to must be after valid_from")


class UpdatePriceListRequest(BaseSchema):
    """Partial update: only the fields you send change (send null to clear a validity date)."""

    name: str | None = Field(default=None, min_length=2, max_length=100)
    list_type: PriceListTypeEnum | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def _window(self) -> "UpdatePriceListRequest":
        _check_window(self.valid_from, self.valid_to)
        return self


class CopyPriceListRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    list_type: PriceListTypeEnum | None = None  # default: same type as the source
    percent: Decimal | None = Field(default=None, gt=Decimal("-100"), le=Decimal("1000"))
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def _window(self) -> "CopyPriceListRequest":
        _check_window(self.valid_from, self.valid_to)
        return self


class BulkPriceItem(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    price: Decimal = Field(validation_alias=AliasChoices("price", "unit_price"))
    min_qty: Decimal = Decimal("1")

    @field_validator("price")
    @classmethod
    def _price(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("price cannot be negative")
        return value

    @field_validator("min_qty")
    @classmethod
    def _min_qty(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("min_qty must be greater than 0")
        return value


class BulkPriceRequest(BaseSchema):
    items: list[BulkPriceItem] = Field(min_length=1)


class BulkPriceResponse(BaseSchema):
    created: int
    updated: int
    unchanged: int
    total: int


class AdjustPricesRequest(BaseSchema):
    percent: Decimal = Field(gt=Decimal("-100"), le=Decimal("1000"))
    category_id: UUID | None = None  # only items of products in this category
    round_to: Decimal | None = Field(default=None, gt=Decimal("0"))  # e.g. 1, 5, 10
    dry_run: bool = False


class AdjustSample(BaseSchema):
    item_id: UUID
    product_id: UUID
    old_price: Decimal
    new_price: Decimal


class AdjustPricesResponse(BaseSchema):
    dry_run: bool
    updated: int
    unchanged: int
    sample: list[AdjustSample]


class PriceListItemDetail(BaseSchema):
    id: UUID
    price_list_id: UUID
    product_id: UUID
    variation_id: UUID | None
    product_name: str
    variation_name: str | None
    sku: str | None
    unit_price: Decimal
    min_qty: Decimal
    updated_at: datetime


class PaginatedPriceListItems(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[PriceListItemDetail]


class AssignPriceListRequest(BaseSchema):
    price_list_id: UUID | None = None  # null clears the assignment
