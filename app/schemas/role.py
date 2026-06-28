from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.base import AuditSchema, BaseSchema


class PermissionItemResponse(BaseSchema):
    permission_key: str
    description: str | None = None
    module: str | None = None


class PermissionModuleGroup(BaseSchema):
    module: str
    permissions: list[PermissionItemResponse]


class PermissionsCatalogResponse(BaseSchema):
    modules: list[PermissionModuleGroup]


class RoleResponse(AuditSchema):
    id: UUID
    business_id: UUID
    name: str
    description: str | None = None
    is_system: bool
    permission_keys: list[str] = Field(default_factory=list)
    permissions: list[PermissionItemResponse] = Field(default_factory=list)


class CreateRoleRequest(BaseSchema):
    name: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    permission_keys: list[str] = Field(default_factory=list)


class UpdateRoleRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "UpdateRoleRequest":
        if self.name is None and self.description is None:
            raise ValueError("At least one of name or description must be provided")
        return self


class AssignPermissionsRequest(BaseSchema):
    permission_keys: list[str] = Field(default_factory=list)

    @field_validator("permission_keys")
    @classmethod
    def dedupe_keys(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for key in value:
            if key not in seen:
                seen.add(key)
                deduped.append(key)
        return deduped
