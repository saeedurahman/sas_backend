from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import AuditSchema, BaseSchema


class BomLineRequest(BaseSchema):
    ingredient_product_id: UUID
    ingredient_variation_id: UUID | None = None
    qty_required: Decimal = Field(gt=0)
    unit_id: UUID | None = None
    wastage_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    sort_order: int = 0


class CreateBomRequest(BaseSchema):
    product_id: UUID
    variation_id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    output_qty: Decimal = Field(default=Decimal("1"), gt=0)
    is_active: bool = True
    lines: list[BomLineRequest] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped


class UpdateBomRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    output_qty: Decimal | None = Field(default=None, gt=0)
    is_active: bool | None = None
    lines: list[BomLineRequest] | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped


class BomLineResponse(BaseSchema):
    id: UUID
    business_id: UUID
    bom_header_id: UUID
    ingredient_product_id: UUID
    ingredient_product_name: str
    ingredient_variation_id: UUID | None = None
    qty_required: Decimal
    unit_id: UUID | None = None
    wastage_pct: Decimal
    sort_order: int


class BomHeaderResponse(AuditSchema):
    id: UUID
    business_id: UUID
    product_id: UUID
    product_name: str
    variation_id: UUID | None = None
    name: str
    output_qty: Decimal
    is_active: bool
    version: int
    lines: list[BomLineResponse] = Field(default_factory=list)


class BomPreviewRequest(BaseSchema):
    bom_header_id: UUID
    qty_to_produce: Decimal = Field(gt=0)


class BomPreviewLineResponse(BaseSchema):
    ingredient_product_id: UUID
    ingredient_product_name: str
    ingredient_variation_id: UUID | None = None
    qty_per_output_unit: Decimal
    total_qty_required: Decimal
    wastage_pct: Decimal


class BomPreviewResponse(BaseSchema):
    bom_header_id: UUID
    product_id: UUID
    output_qty: Decimal
    qty_to_produce: Decimal
    lines: list[BomPreviewLineResponse] = Field(default_factory=list)


class CreateProductionOrderRequest(BaseSchema):
    branch_id: UUID
    bom_header_id: UUID
    qty_to_produce: Decimal = Field(gt=0)
    notes: str | None = None


class UpdateProductionOrderRequest(BaseSchema):
    bom_header_id: UUID | None = None
    qty_to_produce: Decimal | None = Field(default=None, gt=0)
    notes: str | None = None


class ProductionOrderBomSummary(BaseSchema):
    id: UUID
    name: str
    product_id: UUID
    product_name: str
    variation_id: UUID | None = None
    output_qty: Decimal
    version: int


class ProductionLineResponse(BaseSchema):
    id: UUID
    product_id: UUID
    product_name: str
    variation_id: UUID | None = None
    qty_consumed: Decimal
    cost_per_unit: Decimal


class CompleteProductionOrderRequest(BaseSchema):
    qty_produced: Decimal = Field(gt=0)


class ProductionOrderResponse(AuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    bom_header_id: UUID
    production_number: str
    status: str
    qty_to_produce: Decimal
    qty_produced: Decimal
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None
    bom: ProductionOrderBomSummary
    lines: list[ProductionLineResponse] = Field(default_factory=list)
