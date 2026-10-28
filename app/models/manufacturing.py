import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin, TimestampMixin, utc_now
from app.models.enums import production_order_status_enum, sync_status_enum

if TYPE_CHECKING:
    from app.models.business import Branch, Business
    from app.models.inventory import PurchaseLine
    from app.models.product import Product, ProductVariation, Unit


class BomHeader(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "bom_headers"

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
    variation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_variations.id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    output_qty: Mapped[Decimal] = mapped_column(
        Numeric(15, 4), nullable=False, default=1
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")
    lines: Mapped[list["BomLine"]] = relationship(
        back_populates="bom_header",
        lazy="selectin",
    )
    production_orders: Mapped[list["ProductionOrder"]] = relationship(
        back_populates="bom_header",
        lazy="selectin",
    )


class BomLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "bom_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    bom_header_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bom_headers.id", ondelete="CASCADE"),
        nullable=False,
    )
    ingredient_product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ingredient_variation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("product_variations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    qty_required: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("units.id", ondelete="SET NULL"),
        nullable=True,
    )
    wastage_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=0
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
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
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    bom_header: Mapped["BomHeader"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    ingredient_product: Mapped["Product"] = relationship(
        foreign_keys=[ingredient_product_id],
        lazy="selectin",
    )
    ingredient_variation: Mapped["ProductVariation | None"] = relationship(
        foreign_keys=[ingredient_variation_id],
        lazy="selectin",
    )
    unit: Mapped["Unit | None"] = relationship(lazy="selectin")


class ProductionOrder(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "production_orders"

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
    bom_header_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bom_headers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    production_number: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        production_order_status_enum, nullable=False, default="draft"
    )
    qty_to_produce: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    qty_produced: Mapped[Decimal] = mapped_column(
        Numeric(15, 4), nullable=False, default=0
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    bom_header: Mapped["BomHeader"] = relationship(
        back_populates="production_orders",
        lazy="selectin",
    )
    lines: Mapped[list["ProductionLine"]] = relationship(
        back_populates="production_order",
        lazy="selectin",
    )
    cost_layers: Mapped[list["PurchaseLine"]] = relationship(
        back_populates="production_order",
        lazy="selectin",
    )


class ProductionLine(Base, TimestampMixin, SyncMixin):
    __tablename__ = "production_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    production_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    bom_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bom_lines.id", ondelete="SET NULL"),
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
    qty_consumed: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    cost_per_unit: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    business: Mapped["Business"] = relationship(lazy="selectin")
    production_order: Mapped["ProductionOrder"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    bom_line: Mapped["BomLine | None"] = relationship(lazy="selectin")
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")
