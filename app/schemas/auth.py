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
    # Only read when REGISTRATION_CAPTCHA_ENABLED=true.
    captcha_token: str | None = Field(default=None, max_length=4096)

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
    # Scopes the phone lookup to one business. Optional unless
    # LOGIN_REQUIRE_BUSINESS_SLUG=true.
    business_slug: str | None = Field(default=None, min_length=1, max_length=255)

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


class SubscriptionSummary(BaseSchema):
    """Compact subscription state returned with every sign-in."""

    status: str  # trial | active | grace | expired | suspended | cancelled
    days_left: int | None = None


class RefreshTokenResponse(BaseSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    subscription: SubscriptionSummary | None = None


class ChangePasswordRequest(BaseSchema):
    current_password: str = Field(min_length=1)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def check_new_password(cls, value: str) -> str:
        return validate_password(value)


class UserInfo(BaseSchema):
    id: UUID
    full_name: str
    phone: str
    business_id: UUID
    branch_id: UUID | None = None
    business_name: str
    business_type_code: str
    business_slug: str
    roles: list[str] = Field(default_factory=list)
    permission_keys: list[str] = Field(default_factory=list)
    # Fingerprint of roles + permissions + branch scope; changes when access changes.
    permissions_version: str = ""


class MeResponse(UserInfo):
    subscription: SubscriptionSummary | None = None


class TokenResponse(BaseSchema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserInfo
    subscription: SubscriptionSummary | None = None


class MessageResponse(BaseSchema):
    message: str
    success: bool = True
