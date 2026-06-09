from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema
from app.validators import validate_password, validate_phone, validate_pin


class RegisterBusinessRequest(BaseSchema):
    business_name: str = Field(min_length=2, max_length=120)
    business_type_code: str = Field(min_length=2, max_length=50)
    owner_name: str = Field(min_length=2, max_length=100)
    owner_phone: str
    owner_password: str
    branch_name: str = Field(default="Main Branch", min_length=1, max_length=255)
    city: str | None = None

    @field_validator("owner_phone")
    @classmethod
    def check_owner_phone(cls, value: str) -> str:
        return validate_phone(value)

    @field_validator("owner_password")
    @classmethod
    def check_owner_password(cls, value: str) -> str:
        return validate_password(value)


class LoginRequest(BaseSchema):
    phone: str
    password: str

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str) -> str:
        return validate_phone(value)


class PinLoginRequest(BaseSchema):
    business_slug: str = Field(min_length=1, max_length=255)
    user_id: UUID
    pin_code: str

    @field_validator("pin_code")
    @classmethod
    def check_pin(cls, value: str) -> str:
        return validate_pin(value)


class RefreshTokenRequest(BaseSchema):
    refresh_token: str = Field(min_length=1)


class UserInfo(BaseSchema):
    id: UUID
    full_name: str
    phone: str
    business_id: UUID
    branch_id: UUID | None = None
    business_name: str
    business_type_code: str


class TokenResponse(BaseSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserInfo


class MessageResponse(BaseSchema):
    message: str
    success: bool = True
