from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.models.enums import KotStatusEnum, ModifierSelectionTypeEnum, TableStatusEnum
from app.schemas.base import AuditSchema, BaseSchema


class FloorPlanResponse(AuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    name: str
    sort_order: int
    is_active: bool
    layout_json: dict[str, Any] = Field(default_factory=dict)


class CreateFloorPlanRequest(BaseSchema):
    branch_id: UUID
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 0
    layout_json: dict[str, Any] = Field(default_factory=dict)


class UpdateFloorPlanRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = None
    is_active: bool | None = None
    layout_json: dict[str, Any] | None = None


class DiningTableResponse(AuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    floor_plan_id: UUID | None = None
    table_number: str
    capacity: int
    status: TableStatusEnum
    pos_x: Decimal | None = None
    pos_y: Decimal | None = None
    is_active: bool
    active_sale_id: UUID | None = None


class CreateDiningTableRequest(BaseSchema):
    branch_id: UUID
    floor_plan_id: UUID | None = None
    table_number: str = Field(min_length=1, max_length=20)
    capacity: int = Field(default=4, ge=1)
    pos_x: Decimal | None = None
    pos_y: Decimal | None = None

    @field_validator("table_number")
    @classmethod
    def strip_table_number(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("table_number cannot be blank")
        return stripped


class UpdateDiningTableRequest(BaseSchema):
    floor_plan_id: UUID | None = None
    table_number: str | None = Field(default=None, min_length=1, max_length=20)
    capacity: int | None = Field(default=None, ge=1)
    pos_x: Decimal | None = None
    pos_y: Decimal | None = None
    is_active: bool | None = None

    @field_validator("table_number")
    @classmethod
    def strip_table_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("table_number cannot be blank")
        return stripped


class UpdateTableStatusRequest(BaseSchema):
    status: TableStatusEnum
    force: bool = False


class FloorPlanWithTablesResponse(FloorPlanResponse):
    tables: list[DiningTableResponse] = Field(default_factory=list)


class FloorLayoutResponse(BaseSchema):
    branch_id: UUID
    floor_plans: list[FloorPlanWithTablesResponse] = Field(default_factory=list)
    unassigned_tables: list[DiningTableResponse] = Field(default_factory=list)


class ModifierResponse(AuditSchema):
    id: UUID
    business_id: UUID
    modifier_group_id: UUID
    name: str
    price_delta: Decimal
    is_active: bool
    sort_order: int


class ModifierGroupResponse(AuditSchema):
    id: UUID
    business_id: UUID
    name: str
    selection_type: ModifierSelectionTypeEnum
    min_selections: int
    max_selections: int | None = None
    is_required: bool
    sort_order: int
    is_active: bool
    modifiers: list[ModifierResponse] = Field(default_factory=list)


class CreateModifierGroupRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=100)
    selection_type: ModifierSelectionTypeEnum = ModifierSelectionTypeEnum.multiple
    min_selections: int = Field(default=0, ge=0)
    max_selections: int | None = Field(default=None, ge=1)
    is_required: bool = False
    sort_order: int = 0

    @model_validator(mode="after")
    def validate_selection_bounds(self) -> "CreateModifierGroupRequest":
        if (
            self.max_selections is not None
            and self.max_selections < self.min_selections
        ):
            raise ValueError("max_selections must be >= min_selections")
        if self.selection_type == ModifierSelectionTypeEnum.single:
            if self.max_selections is not None and self.max_selections > 1:
                raise ValueError("single selection groups cannot have max_selections > 1")
        return self


class UpdateModifierGroupRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    selection_type: ModifierSelectionTypeEnum | None = None
    min_selections: int | None = Field(default=None, ge=0)
    max_selections: int | None = Field(default=None, ge=1)
    is_required: bool | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class CreateModifierRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=100)
    price_delta: Decimal = Decimal("0")
    sort_order: int = 0


class UpdateModifierRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    price_delta: Decimal | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ReplaceProductModifierGroupsRequest(BaseSchema):
    modifier_group_ids: list[UUID] = Field(default_factory=list)


class ValidatedModifierSnapshot(BaseSchema):
    modifier_id: UUID
    name: str
    price_delta: Decimal


class ValidateLineModifiersRequest(BaseSchema):
    product_id: UUID
    modifier_ids: list[UUID] = Field(default_factory=list)


class KotOrderLineResponse(BaseSchema):
    id: UUID
    kot_order_id: UUID
    sale_line_id: UUID | None = None
    product_id: UUID
    product_name: str
    variation_id: UUID | None = None
    qty: Decimal
    modifiers_json: list[Any] = Field(default_factory=list)
    kitchen_notes: str | None = None
    status: str


class KotOrderResponse(BaseSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID
    sale_id: UUID | None = None
    table_id: UUID | None = None
    table_number: str | None = None
    kot_number: str
    status: str
    fired_at: datetime
    ready_at: datetime | None = None
    served_at: datetime | None = None
    notes: str | None = None
    lines: list[KotOrderLineResponse] = Field(default_factory=list)


class UpdateKotOrderStatusRequest(BaseSchema):
    status: KotStatusEnum


class UpdateKotOrderLineStatusRequest(BaseSchema):
    status: KotStatusEnum

