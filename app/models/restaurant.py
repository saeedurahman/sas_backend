import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin, TimestampMixin, utc_now
from app.models.enums import (
    kot_status_enum,
    modifier_selection_type_enum,
    sync_status_enum,
    table_status_enum,
)

if TYPE_CHECKING:
    from app.models.business import Branch, Business
    from app.models.product import Product, ProductVariation
    from app.models.sales import Sale, SaleLine


class FloorPlan(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "floor_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    layout_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    tables: Mapped[list["DiningTable"]] = relationship(
        back_populates="floor_plan",
        lazy="selectin",
    )


class DiningTable(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "tables"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
    )
    floor_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("floor_plans.id", ondelete="SET NULL"),
        nullable=True,
    )
    table_number: Mapped[str] = mapped_column(String(20), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    status: Mapped[str] = mapped_column(
        table_status_enum, nullable=False, default="available"
    )
    pos_x: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    pos_y: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    floor_plan: Mapped["FloorPlan | None"] = relationship(
        back_populates="tables",
        lazy="selectin",
    )
    sales: Mapped[list["Sale"]] = relationship(
        back_populates="dining_table",
        lazy="selectin",
    )
    kot_orders: Mapped[list["KotOrder"]] = relationship(
        back_populates="dining_table",
        lazy="selectin",
    )


class ModifierGroup(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "modifier_groups"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    selection_type: Mapped[str] = mapped_column(
        modifier_selection_type_enum, nullable=False, default="multiple"
    )
    min_selections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_selections: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    modifiers: Mapped[list["Modifier"]] = relationship(
        back_populates="modifier_group",
        lazy="selectin",
    )
    product_links: Mapped[list["ProductModifierGroup"]] = relationship(
        back_populates="modifier_group",
        lazy="selectin",
    )


class Modifier(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "modifiers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    modifier_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("modifier_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_delta: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    modifier_group: Mapped["ModifierGroup"] = relationship(
        back_populates="modifiers",
        lazy="selectin",
    )


class ProductModifierGroup(Base):
    __tablename__ = "product_modifier_groups"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    )
    modifier_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("modifier_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    product: Mapped["Product"] = relationship(lazy="selectin")
    modifier_group: Mapped["ModifierGroup"] = relationship(
        back_populates="product_links",
        lazy="selectin",
    )


class KotOrder(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "kot_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sale_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sales.id", ondelete="SET NULL"),
        nullable=True,
    )
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tables.id", ondelete="SET NULL"),
        nullable=True,
    )
    kot_number: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        kot_status_enum, nullable=False, default="pending"
    )
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    served_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    sale: Mapped["Sale | None"] = relationship(
        back_populates="kot_orders",
        lazy="selectin",
    )
    dining_table: Mapped["DiningTable | None"] = relationship(
        back_populates="kot_orders",
        lazy="selectin",
    )
    lines: Mapped[list["KotOrderLine"]] = relationship(
        back_populates="kot_order",
        lazy="selectin",
    )


class KotOrderLine(Base, TimestampMixin, SyncMixin):
    __tablename__ = "kot_order_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    kot_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("kot_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    sale_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sale_lines.id", ondelete="SET NULL"),
        nullable=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    variation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_variations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    modifiers_json: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    kitchen_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        kot_status_enum, nullable=False, default="pending"
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    business: Mapped["Business"] = relationship(lazy="selectin")
    kot_order: Mapped["KotOrder"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    sale_line: Mapped["SaleLine | None"] = relationship(lazy="selectin")
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")
