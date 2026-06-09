from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.base import AuditSchema, BaseSchema
from app.validators import validate_password, validate_phone, validate_pin


class PermissionResponse(BaseSchema):
    id: UUID
    permission_key: str
    name: str
    description: str | None = None
    module: str | None = None

    @model_validator(mode="before")
    @classmethod
    def map_permission_fields(cls, data: object) -> object:
        if not hasattr(data, "permission_key"):
            return data
        return {
            "id": data.id,
            "permission_key": data.permission_key,
            "name": data.permission_key,
            "description": data.description,
            "module": data.module,
        }


class RoleResponse(BaseSchema):
    id: UUID
    name: str
    description: str | None = None
    is_active: bool
    permissions: list[PermissionResponse] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def map_role_fields(cls, data: object) -> object:
        if not hasattr(data, "role_permissions"):
            return data
        permissions = [
            PermissionResponse.model_validate(rp.permission)
            for rp in data.role_permissions
            if rp.permission is not None
        ]
        return {
            "id": data.id,
            "name": data.name,
            "description": data.description,
            "is_active": data.deleted_at is None,
            "permissions": permissions,
        }


class UserResponse(AuditSchema):
    id: UUID
    business_id: UUID
    branch_id: UUID | None = None
    full_name: str
    phone: str
    is_active: bool
    is_locked: bool
    last_login_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def map_user_fields(cls, data: object) -> object:
        if not hasattr(data, "status"):
            return data
        return {
            "id": data.id,
            "business_id": data.business_id,
            "branch_id": data.default_branch_id,
            "full_name": data.full_name,
            "phone": data.phone or "",
            "is_active": data.status == "active" and data.deleted_at is None,
            "is_locked": data.is_locked,
            "last_login_at": data.last_login_at,
            "created_at": data.created_at,
            "updated_at": data.updated_at,
            "deleted_at": data.deleted_at,
        }


class CreateUserRequest(BaseSchema):
    full_name: str = Field(min_length=2, max_length=100)
    phone: str
    password: str
    branch_id: UUID | None = None
    role_ids: list[UUID] = Field(default_factory=list)

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str) -> str:
        return validate_phone(value)

    @field_validator("password")
    @classmethod
    def check_password(cls, value: str) -> str:
        return validate_password(value)


class UpdateUserRequest(BaseSchema):
    full_name: str | None = Field(default=None, min_length=2, max_length=100)
    branch_id: UUID | None = None
    is_active: bool | None = None


class SetPinRequest(BaseSchema):
    pin_code: str

    @field_validator("pin_code")
    @classmethod
    def check_pin(cls, value: str) -> str:
        return validate_pin(value)
