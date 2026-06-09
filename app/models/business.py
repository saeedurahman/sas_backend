import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, String, Text, Uuid
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    AuditMixin,
    Base,
    SoftDeleteMixin,
    SyncMixin,
    TimestampMixin,
)
from app.models.enums import (
    subscription_plan_enum,
    subscription_status_enum,
    sync_status_enum,
)

if TYPE_CHECKING:
    from app.models.user import User


class BusinessType(Base, TimestampMixin):
    __tablename__ = "business_types"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int | None] = mapped_column(SmallInteger, default=0)

    businesses: Mapped[list["Business"]] = relationship(
        back_populates="business_type",
        lazy="selectin",
    )


class Business(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("business_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tax_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country_code: Mapped[str] = mapped_column(CHAR(2), nullable=False, default="PK")
    currency_code: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="PKR")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Karachi"
    )
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    subscription_plan: Mapped[str] = mapped_column(
        subscription_plan_enum, nullable=False, default="trial"
    )
    subscription_status: Mapped[str] = mapped_column(
        subscription_status_enum, nullable=False, default="trial"
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscription_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business_type: Mapped["BusinessType"] = relationship(
        back_populates="businesses",
        lazy="selectin",
    )
    config: Mapped["BusinessConfig | None"] = relationship(
        back_populates="business",
        lazy="selectin",
        uselist=False,
    )
    branches: Mapped[list["Branch"]] = relationship(
        back_populates="business",
        lazy="selectin",
    )
    users: Mapped[list["User"]] = relationship(
        back_populates="business",
        lazy="selectin",
    )


class BusinessConfig(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "business_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # DEPRECATED: use config_json instead
    enable_restaurant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # DEPRECATED: use config_json instead
    enable_manufacturing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    enable_loyalty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # DEPRECATED: use config_json instead
    enable_multi_price_list: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    enable_batch_tracking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    enable_expiry_tracking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    enable_weight_billing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    enable_table_management: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    enable_kot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # DEPRECATED: use config_json instead
    enable_offline_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # DEPRECATED: use config_json instead
    enable_accounting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # DEPRECATED: use config_json instead
    default_tax_inclusive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    allow_negative_stock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # DEPRECATED: use config_json instead
    fifo_costing_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    receipt_prefix: Mapped[str | None] = mapped_column(String(20), default="RCP")
    invoice_prefix: Mapped[str | None] = mapped_column(String(20), default="INV")
    po_prefix: Mapped[str | None] = mapped_column(String(20), default="PO")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(
        back_populates="config",
        lazy="selectin",
    )


class Branch(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "branches"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_head_office: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(
        back_populates="branches",
        lazy="selectin",
    )
