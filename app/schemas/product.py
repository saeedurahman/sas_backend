from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.models.enums import PriceListTypeEnum, ProductTypeEnum, TrackingTypeEnum
from app.schemas.base import BaseSchema


class ProductAuditSchema(BaseSchema):
    """Audit timestamps only — deleted_at and sync fields excluded from API."""

    created_at: datetime
    updated_at: datetime


# ── Request schemas ──────────────────────────────────────────────────────────


class CreateCategoryRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    parent_id: UUID | None = None
    sort_order: int = 0
    slug: str | None = Field(
        default=None,
        description="Auto-generated from name in service if not provided",
    )


class UpdateCategoryRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    parent_id: UUID | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class CreateBrandRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)


class UpdateBrandRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    is_active: bool | None = None


class CreateUnitRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=50)
    symbol: str = Field(min_length=1, max_length=10)
    is_base_unit: bool = False


class UpdateUnitRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    symbol: str | None = Field(default=None, min_length=1, max_length=10)
    is_active: bool | None = None


class CreateUnitConversionRequest(BaseSchema):
    from_unit_id: UUID
    to_unit_id: UUID
    conversion_factor: Decimal

    @field_validator("conversion_factor")
    @classmethod
    def validate_conversion_factor(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("conversion_factor must be greater than 0")
        return value


class CreateVariationRequest(BaseSchema):
    # Variation SKU: unique within business (ignore soft-deleted) — enforced in service.
    name: str = Field(min_length=1, max_length=100)
    sku: str | None = None
    unit_id: UUID | None = None
    is_default: bool = False
    weight_grams: Decimal | None = None

    @field_validator("weight_grams")
    @classmethod
    def validate_weight(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("weight_grams must be greater than 0")
        return value


class UpdateVariationRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    sku: str | None = None
    unit_id: UUID | None = None
    is_active: bool | None = None
    weight_grams: Decimal | None = None

    @field_validator("weight_grams")
    @classmethod
    def validate_weight(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("weight_grams must be greater than 0")
        return value


class CreateProductRequest(BaseSchema):
    # Product SKU: unique within business (ignore soft-deleted) — enforced in service.
    name: str = Field(min_length=2, max_length=255)
    category_id: UUID | None = None
    brand_id: UUID | None = None
    base_unit_id: UUID
    sku: str | None = None
    product_type: ProductTypeEnum
    tracking_type: TrackingTypeEnum
    is_sellable: bool = True
    is_purchasable: bool = True
    description: str | None = None
    image_url: str | None = None
    shelf_life_days: int | None = None
    min_stock_level: Decimal | None = None
    max_stock_level: Decimal | None = None
    variations: list[CreateVariationRequest] = Field(
        default_factory=list,
        description="If empty, service creates a default variation",
    )

    @field_validator("shelf_life_days")
    @classmethod
    def validate_shelf_life(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("shelf_life_days must be greater than 0")
        return value


class UpdateProductRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    category_id: UUID | None = None
    brand_id: UUID | None = None
    sku: str | None = None
    is_sellable: bool | None = None
    is_purchasable: bool | None = None
    is_active: bool | None = None
    description: str | None = None
    image_url: str | None = None
    shelf_life_days: int | None = None
    min_stock_level: Decimal | None = None
    max_stock_level: Decimal | None = None

    @field_validator("shelf_life_days")
    @classmethod
    def validate_shelf_life(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("shelf_life_days must be greater than 0")
        return value


class CreateBarcodeRequest(BaseSchema):
    # Barcode: unique within business (ignore soft-deleted) — enforced in service.
    barcode: str = Field(min_length=4, max_length=100)
    barcode_type: str = "EAN13"
    variation_id: UUID | None = None
    is_primary: bool = False


class CreatePriceListRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    list_type: PriceListTypeEnum
    is_default: bool = False
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def validate_date_range(self) -> "CreatePriceListRequest":
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_to <= self.valid_from:
                raise ValueError("valid_to must be after valid_from")
        return self


class SetPriceRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    unit_price: Decimal
    min_qty: Decimal = Decimal("1")

    @field_validator("unit_price")
    @classmethod
    def validate_unit_price(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Price cannot be negative")
        return value

    @field_validator("min_qty")
    @classmethod
    def validate_min_qty(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("min_qty must be greater than 0")
        return value


# ── Response schemas (child before parent) ───────────────────────────────────


class UnitResponse(ProductAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    symbol: str
    is_base_unit: bool
    is_active: bool

    @model_validator(mode="before")
    @classmethod
    def map_unit_fields(cls, data: object) -> object:
        if not hasattr(data, "deleted_at"):
            return data
        return {
            "id": data.id,
            "business_id": data.business_id,
            "name": data.name,
            "symbol": data.symbol,
            "is_base_unit": data.is_base_unit,
            "is_active": data.deleted_at is None,
            "created_at": data.created_at,
            "updated_at": data.updated_at,
        }


class UnitConversionResponse(ProductAuditSchema):
    id: UUID
    business_id: UUID
    from_unit_id: UUID
    to_unit_id: UUID
    conversion_factor: Decimal


class BrandResponse(ProductAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    is_active: bool


class BarcodeResponse(BaseSchema):
    id: UUID
    product_id: UUID
    variation_id: UUID | None
    barcode: str
    barcode_type: str
    is_primary: bool


class ProductLocationResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    product_id: UUID
    variation_id: UUID | None
    is_available: bool
    min_stock_level: Decimal | None
    max_stock_level: Decimal | None
    bin_location: str | None


class VariationResponse(ProductAuditSchema):
    id: UUID
    business_id: UUID
    product_id: UUID
    name: str
    sku: str | None
    unit_id: UUID | None
    is_default: bool
    is_active: bool
    weight_grams: Decimal | None
    barcodes: list[BarcodeResponse] = Field(default_factory=list)
    unit: UnitResponse | None = None


class CategoryResponse(ProductAuditSchema):
    id: UUID
    business_id: UUID
    parent_id: UUID | None
    name: str
    slug: str | None
    sort_order: int
    is_active: bool
    children: list["CategoryResponse"] = Field(default_factory=list)


CategoryResponse.model_rebuild()


class PriceListItemResponse(BaseSchema):
    id: UUID
    business_id: UUID
    price_list_id: UUID
    product_id: UUID
    variation_id: UUID | None
    unit_price: Decimal
    min_qty: Decimal


class PriceListResponse(ProductAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    list_type: str
    is_default: bool
    is_active: bool
    valid_from: datetime | None
    valid_to: datetime | None
    items: list[PriceListItemResponse] = Field(default_factory=list)


class ProductListResponse(BaseSchema):
    id: UUID
    business_id: UUID
    name: str
    sku: str | None
    product_type: str
    tracking_type: str
    is_sellable: bool
    is_purchasable: bool
    is_active: bool
    category_id: UUID | None
    brand_id: UUID | None


class PaginatedProductResponse(BaseSchema):
    total: int
    skip: int
    limit: int
    items: list[ProductListResponse]


class ProductResponse(ProductAuditSchema):
    id: UUID
    business_id: UUID
    name: str
    sku: str | None
    product_type: str
    tracking_type: str
    is_sellable: bool
    is_purchasable: bool
    is_active: bool
    description: str | None
    image_url: str | None
    shelf_life_days: int | None
    min_stock_level: Decimal | None
    max_stock_level: Decimal | None
    category: CategoryResponse | None = None
    brand: BrandResponse | None = None
    base_unit: UnitResponse | None = None
    variations: list[VariationResponse] = Field(default_factory=list)
    barcodes: list[BarcodeResponse] = Field(default_factory=list)
