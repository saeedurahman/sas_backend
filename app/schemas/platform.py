"""Request/response shapes for /platform/* (admin panel API)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AliasChoices, EmailStr, Field, PlainSerializer, model_validator

from app.schemas.base import BaseSchema

# PKR amounts go out as JSON numbers (the panel formats them).
Money = Annotated[
    Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")
]

PaymentMethod = Literal["jazzcash", "easypaisa", "bank", "cash"]
BusinessStatus = Literal["trial", "active", "grace", "expired", "suspended", "cancelled"]


# ── auth ──────────────────────────────────────────────────────────────────────
class PlatformLoginRequest(BaseSchema):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    totp_code: str = Field(min_length=6, max_length=6)


class AdminOut(BaseSchema):
    id: UUID
    email: str
    name: str
    is_active: bool = True
    last_login_at: datetime | None = Field(
        default=None, validation_alias=AliasChoices("last_login_at", "last_login")
    )


class PlatformTokenResponse(BaseSchema):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    admin: AdminOut


class MessageOut(BaseSchema):
    message: str


# ── dashboard ─────────────────────────────────────────────────────────────────
class AttentionItem(BaseSchema):
    business_id: UUID
    name: str
    owner_phone: str | None = None
    status: BusinessStatus
    expires_at: datetime | None = None
    days_left: int | None = None


class DashboardOut(BaseSchema):
    active: int
    trial: int
    expiring_7d: int
    grace: int
    suspended: int
    expired: int
    cancelled: int
    payments_this_month_pkr: Money
    needs_attention: list[AttentionItem]


# ── businesses ────────────────────────────────────────────────────────────────
class BusinessRow(BaseSchema):
    id: UUID
    name: str
    owner_phone: str | None = None
    plan_id: UUID | None = None
    plan_name: str | None = None
    status: BusinessStatus
    expires_at: datetime | None = None
    days_left: int | None = None
    branches: int
    users: int
    last_active_at: datetime | None = None
    created_at: datetime


class BusinessPage(BaseSchema):
    items: list[BusinessRow]
    total: int
    page: int
    page_size: int


class SubscriptionOut(BaseSchema):
    plan_id: UUID | None = None
    plan_name: str | None = None
    status: BusinessStatus
    stored_status: str  # raw column: trial|active|past_due|suspended|cancelled
    trial_ends_at: datetime | None = None
    subscription_ends_at: datetime | None = None
    expires_at: datetime | None = None
    days_left: int | None = None
    in_grace: bool


class PaymentOut(BaseSchema):
    id: UUID
    business_id: UUID
    plan_id: UUID | None = None
    amount_pkr: Money
    method: PaymentMethod
    reference: str | None = None
    note: str | None = None
    period_months: int
    period_start: datetime
    period_end: datetime
    recorded_by: UUID | None = None
    recorded_by_email: str | None = None
    created_at: datetime


class AuditOut(BaseSchema):
    id: UUID
    created_at: datetime
    admin_id: UUID | None = None
    admin_email: str
    action: str
    entity_type: str
    entity_id: str | None = None
    entity_label: str | None = None
    business_id: UUID | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    ip_address: str | None = None


class AuditPage(BaseSchema):
    items: list[AuditOut]
    total: int
    page: int
    page_size: int


class NoteOut(BaseSchema):
    id: UUID
    text: str
    admin_email: str
    created_at: datetime


class BusinessDetail(BaseSchema):
    id: UUID
    name: str
    owner_name: str | None = None
    owner_phone: str | None = None
    owner_email: str | None = None
    address: str | None = None
    city: str | None = None
    is_active: bool
    created_at: datetime
    branches: int
    users: int
    last_active_at: datetime | None = None
    subscription: SubscriptionOut
    payments: list[PaymentOut]
    recent_audit: list[AuditOut]
    notes: list[NoteOut]


class SubscriptionPatch(BaseSchema):
    plan_id: UUID | None = None
    status: Literal["trial", "active", "suspended", "cancelled"] | None = None
    extend_days: int | None = Field(default=None, ge=1, le=3650)
    subscription_ends_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _one_way_to_move_the_date(self) -> "SubscriptionPatch":
        if self.extend_days is not None and self.subscription_ends_at is not None:
            raise ValueError("Send extend_days or subscription_ends_at, not both")
        if (
            self.plan_id is None
            and self.status is None
            and self.extend_days is None
            and self.subscription_ends_at is None
        ):
            raise ValueError("Nothing to change")
        return self


class PaymentCreate(BaseSchema):
    amount_pkr: Decimal = Field(
        gt=0, max_digits=12, decimal_places=2,
        validation_alias=AliasChoices("amount_pkr", "amount"),
    )
    method: PaymentMethod
    reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)
    period_months: int = Field(
        ge=1, le=36, validation_alias=AliasChoices("period_months", "months")
    )
    plan_id: UUID | None = None


class PaymentResult(BaseSchema):
    payment: PaymentOut
    subscription: SubscriptionOut


class ReasonBody(BaseSchema):
    reason: str | None = Field(default=None, max_length=1000)


class NoteCreate(BaseSchema):
    text: str = Field(min_length=1, max_length=4000)


# ── plans ─────────────────────────────────────────────────────────────────────
class PlanIn(BaseSchema):
    name: str = Field(min_length=1, max_length=100)
    monthly_price_pkr: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    onetime_price_pkr: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    notes: str | None = Field(default=None, max_length=2000)


class PlanOut(BaseSchema):
    id: UUID
    name: str
    monthly_price_pkr: Money
    onetime_price_pkr: Money
    notes: str | None = None
    is_active: bool
    business_count: int = 0


# ── settings ──────────────────────────────────────────────────────────────────
class PlatformSettingsBody(BaseSchema):
    contact_phone: str = Field(default="", max_length=50)
    contact_whatsapp: str = Field(default="", max_length=50)
    contact_email: str = Field(default="", max_length=255)
    payment_instructions: str = Field(default="", max_length=5000)


# ── platform admins ───────────────────────────────────────────────────────────
class AdminCreate(BaseSchema):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)


class AdminCredentials(BaseSchema):
    """Shown once: the 2FA secret cannot be read back later."""

    admin: AdminOut
    totp_secret: str
    provisioning_uri: str


class Reset2faResult(BaseSchema):
    totp_secret: str
    provisioning_uri: str


# ── licenses ──────────────────────────────────────────────────────────────────
class LicenseIssue(BaseSchema):
    customer_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    machine_id: str = Field(min_length=3, max_length=200)
    updates_until: date
    notes: str | None = Field(default=None, max_length=2000)


class LicenseOut(BaseSchema):
    id: UUID
    license_id: str
    customer_name: str
    phone: str | None = None
    email: str | None = None
    edition: str
    machine_id: str
    issued_at: datetime
    updates_until: date
    status: Literal["issued", "active", "revoked"]
    transfer_count: int
    previous_license_id: UUID | None = None
    revoked_at: datetime | None = None
    notes: str | None = None
    key_string: str


class LicensePage(BaseSchema):
    items: list[LicenseOut]
    total: int
    page: int
    page_size: int


class LicenseTransfer(BaseSchema):
    machine_id: str = Field(min_length=3, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)


class LicenseExtend(BaseSchema):
    updates_until: date


class LicenseTransferResult(BaseSchema):
    old: LicenseOut
    new: LicenseOut


class RevokedEntry(BaseSchema):
    license_id: str
    revoked_at: datetime


class RevocationList(BaseSchema):
    v: int = 1
    issued_at: datetime
    revoked: list[RevokedEntry]
    signature: str
