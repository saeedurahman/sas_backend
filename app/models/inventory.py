import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin
from app.models.enums import (
    adjustment_reason_enum,
    purchase_order_status_enum,
    reference_type_enum,
    stock_movement_type_enum,
    sync_status_enum,
    transfer_status_enum,
)

if TYPE_CHECKING:
    from app.models.business import Branch, Business
    from app.models.manufacturing import ProductionOrder
    from app.models.product import Product, ProductVariation


class Supplier(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(
        back_populates="supplier",
        lazy="selectin",
    )
    purchase_receipts: Mapped[list["PurchaseReceipt"]] = relationship(
        back_populates="supplier",
        lazy="selectin",
    )


class PurchaseOrder(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "purchase_orders"

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
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    po_number: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        purchase_order_status_enum, nullable=False, default="draft"
    )
    ordered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    supplier: Mapped["Supplier"] = relationship(
        back_populates="purchase_orders",
        lazy="selectin",
    )
    lines: Mapped[list["PurchaseLine"]] = relationship(
        back_populates="purchase_order",
        lazy="selectin",
    )
    receipts: Mapped[list["PurchaseReceipt"]] = relationship(
        back_populates="purchase_order",
        lazy="selectin",
    )


class PurchaseLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "purchase_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=True,
    )
    production_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_orders.id", ondelete="SET NULL"),
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
    ordered_qty: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    received_qty: Mapped[Decimal] = mapped_column(
        Numeric(15, 4), nullable=False, default=0
    )
    qty_remaining: Mapped[Decimal] = mapped_column(
        Numeric(15, 4), nullable=False, default=0
    )
    cost_per_unit: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    batch_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    purchase_order: Mapped["PurchaseOrder | None"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    production_order: Mapped["ProductionOrder | None"] = relationship(
        back_populates="cost_layers",
        lazy="selectin",
    )
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")
    stock_movements: Mapped[list["StockMovement"]] = relationship(
        back_populates="purchase_line",
        lazy="selectin",
    )


class PurchaseReceipt(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "purchase_receipts"

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
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    receipt_number: Mapped[str] = mapped_column(String(50), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    supplier_invoice_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    supplier: Mapped["Supplier"] = relationship(
        back_populates="purchase_receipts",
        lazy="selectin",
    )
    purchase_order: Mapped["PurchaseOrder | None"] = relationship(
        back_populates="receipts",
        lazy="selectin",
    )
    lines: Mapped[list["PurchaseReceiptLine"]] = relationship(
        back_populates="purchase_receipt",
        lazy="selectin",
    )


class PurchaseReceiptLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "purchase_receipt_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    purchase_receipt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_receipts.id", ondelete="CASCADE"),
        nullable=False,
    )
    purchase_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_lines.id", ondelete="SET NULL"),
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
    qty_received: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    cost_per_unit: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    batch_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    purchase_receipt: Mapped["PurchaseReceipt"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    purchase_line: Mapped["PurchaseLine | None"] = relationship(lazy="selectin")
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")


class StockMovement(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "stock_movements"

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
    movement_type: Mapped[str] = mapped_column(
        stock_movement_type_enum, nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    cost_per_unit: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    reference_type: Mapped[str] = mapped_column(reference_type_enum, nullable=False)
    reference_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    purchase_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_lines.id", ondelete="SET NULL"),
        nullable=True,
    )
    batch_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    movement_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    movement_sequence: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")
    purchase_line: Mapped["PurchaseLine | None"] = relationship(
        back_populates="stock_movements",
        lazy="selectin",
    )


class StockAdjustment(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "stock_adjustments"

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
    adjustment_number: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(
        adjustment_reason_enum, nullable=False, default="count_correction"
    )
    adjusted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    lines: Mapped[list["StockAdjustmentLine"]] = relationship(
        back_populates="stock_adjustment",
        lazy="selectin",
    )


class StockAdjustmentLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "stock_adjustment_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    stock_adjustment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("stock_adjustments.id", ondelete="CASCADE"),
        nullable=False,
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
    qty_delta: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    cost_per_unit: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    stock_adjustment: Mapped["StockAdjustment"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")


class StockTransfer(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "stock_transfers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    transfer_number: Mapped[str] = mapped_column(String(50), nullable=False)
    source_branch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dest_branch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        transfer_status_enum, nullable=False, default="draft"
    )
    transferred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    business: Mapped["Business"] = relationship(lazy="selectin")
    source_branch: Mapped["Branch"] = relationship(
        foreign_keys=[source_branch_id],
        lazy="selectin",
    )
    dest_branch: Mapped["Branch"] = relationship(
        foreign_keys=[dest_branch_id],
        lazy="selectin",
    )
    lines: Mapped[list["StockTransferLine"]] = relationship(
        back_populates="stock_transfer",
        lazy="selectin",
    )


class StockTransferLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "stock_transfer_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    stock_transfer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("stock_transfers.id", ondelete="CASCADE"),
        nullable=False,
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
    cost_per_unit: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    qty_received: Mapped[Decimal] = mapped_column(
        Numeric(15, 4), nullable=False, default=0
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    stock_transfer: Mapped["StockTransfer"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")


class WasteEntry(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "waste_entries"

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
    waste_number: Mapped[str] = mapped_column(String(50), nullable=False)
    wasted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(
        adjustment_reason_enum, nullable=False, default="expiry"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    lines: Mapped[list["WasteEntryLine"]] = relationship(
        back_populates="waste_entry",
        lazy="selectin",
    )


class WasteEntryLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "waste_entry_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    waste_entry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("waste_entries.id", ondelete="CASCADE"),
        nullable=False,
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
    cost_per_unit: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    batch_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    waste_entry: Mapped["WasteEntry"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")
