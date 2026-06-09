from typing import Any
from uuid import UUID

from pydantic import EmailStr, Field, field_validator, model_validator

from app.schemas.base import AuditSchema, BaseSchema


class BusinessTypeResponse(BaseSchema):
    id: UUID
    code: str
    name: str
    description: str | None = None
    is_active: bool


class BranchResponse(AuditSchema):
    id: UUID
    business_id: UUID
    name: str
    address: str | None = None
    phone: str | None = None
    is_default: bool
    is_active: bool

    @model_validator(mode="before")
    @classmethod
    def map_branch_fields(cls, data: object) -> object:
        if not hasattr(data, "address_line1"):
            return data
        return {
            "id": data.id,
            "business_id": data.business_id,
            "name": data.name,
            "address": data.address_line1,
            "phone": data.phone,
            "is_default": data.is_head_office,
            "is_active": data.is_active,
            "created_at": data.created_at,
            "updated_at": data.updated_at,
            "deleted_at": data.deleted_at,
        }


class CreateBranchRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    address: str | None = None
    phone: str | None = None

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        from app.validators import validate_phone

        return validate_phone(value)


class UpdateBranchRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    address: str | None = None
    phone: str | None = None
    is_active: bool | None = None

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        from app.validators import validate_phone

        return validate_phone(value)


class BusinessConfigResponse(BaseSchema):
    id: UUID
    business_id: UUID
    enable_restaurant: bool
    enable_manufacturing: bool
    enable_weight_billing: bool
    enable_kot: bool
    fifo_costing_enabled: bool
    allow_negative_stock: bool
    config_json: dict[str, Any] = Field(default_factory=dict)


class UpdateConfigRequest(BaseSchema):
    config_json: dict[str, Any]


class BusinessResponse(AuditSchema):
    id: UUID
    name: str
    legal_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    city: str | None = None
    country_code: str
    currency_code: str
    subscription_plan: str
    subscription_status: str
    is_active: bool
    business_type: BusinessTypeResponse
    config: BusinessConfigResponse
    branches: list[BranchResponse] = Field(default_factory=list)


class UpdateBusinessRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    legal_name: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
