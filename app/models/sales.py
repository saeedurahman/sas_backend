import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin
from app.models.enums import (
    discount_type_enum,
    ledger_entry_type_enum,
    payment_method_enum,
    payment_status_enum,
    reference_type_enum,
    sale_status_enum,
    sale_type_enum,
    sync_status_enum,
)

if TYPE_CHECKING:
    from app.models.business import Branch, Business
    from app.models.product import Product, ProductVariation
    from app.models.restaurant import DiningTable, KotOrder


class Customer(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    ledger_entries: Mapped[list["CustomerLedger"]] = relationship(
        back_populates="customer",
        lazy="selectin",
    )
    sales: Mapped[list["Sale"]] = relationship(
        back_populates="customer",
        lazy="selectin",
    )


class CustomerLedger(Base, SoftDeleteMixin, SyncMixin):
    """Append-only customer account ledger.

    Sign convention:
      - Positive amount: money received from customer (payment, opening credit).
      - Negative amount: money owed by customer (sale on credit, debit adjustment).
      - customer_balance = SUM(amount) per customer.
      - Positive balance: customer has credit.
      - Negative balance: customer owes money.
    """

    __tablename__ = "customer_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_type: Mapped[str] = mapped_column(ledger_entry_type_enum, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(
        reference_type_enum, nullable=True
    )
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    entry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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

    business: Mapped["Business"] = relationship(lazy="selectin")
    customer: Mapped["Customer"] = relationship(
        back_populates="ledger_entries",
        lazy="selectin",
    )


class TaxRate(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "tax_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    is_compound: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")


class DiscountScheme(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "discount_schemes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    discount_type: Mapped[str] = mapped_column(discount_type_enum, nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    min_purchase_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    applies_to_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")


class Sale(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "sales"

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
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    register_shift_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    price_list_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("price_lists.id", ondelete="SET NULL"),
        nullable=True,
    )
    sale_number: Mapped[str] = mapped_column(String(50), nullable=False)
    sale_type: Mapped[str] = mapped_column(
        sale_type_enum, nullable=False, default="pos"
    )
    status: Mapped[str] = mapped_column(
        sale_status_enum, nullable=False, default="draft"
    )
    sold_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_scheme_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discount_schemes.id", ondelete="SET NULL"),
        nullable=True,
    )
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tables.id", ondelete="SET NULL"),
        nullable=True,
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="pending"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    customer: Mapped["Customer | None"] = relationship(
        back_populates="sales",
        lazy="selectin",
    )
    discount_scheme: Mapped["DiscountScheme | None"] = relationship(lazy="selectin")
    lines: Mapped[list["SaleLine"]] = relationship(
        back_populates="sale",
        lazy="selectin",
    )
    payments: Mapped[list["SalePayment"]] = relationship(
        back_populates="sale",
        lazy="selectin",
    )
    returns: Mapped[list["SaleReturn"]] = relationship(
        back_populates="original_sale",
        lazy="selectin",
    )
    dining_table: Mapped["DiningTable | None"] = relationship(
        back_populates="sales",
        lazy="selectin",
    )
    kot_orders: Mapped[list["KotOrder"]] = relationship(
        back_populates="sale",
        lazy="selectin",
    )


class SaleLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "sale_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    sale_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sales.id", ondelete="CASCADE"),
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
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    discount_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=0
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    cost_per_unit: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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

    sale: Mapped["Sale"] = relationship(back_populates="lines", lazy="selectin")
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")


class SalePayment(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "sale_payments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    sale_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sales.id", ondelete="CASCADE"),
        nullable=False,
    )
    payment_method: Mapped[str] = mapped_column(
        payment_method_enum, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        payment_status_enum, nullable=False, default="completed"
    )
    reference_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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

    sale: Mapped["Sale"] = relationship(back_populates="payments", lazy="selectin")


class SaleReturn(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "sale_returns"

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
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    return_number: Mapped[str] = mapped_column(String(50), nullable=False)
    returned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    original_sale: Mapped["Sale | None"] = relationship(
        back_populates="returns",
        foreign_keys=[sale_id],
        lazy="selectin",
    )
    customer: Mapped["Customer | None"] = relationship(lazy="selectin")
    lines: Mapped[list["SaleReturnLine"]] = relationship(
        back_populates="sale_return",
        lazy="selectin",
    )
    payments: Mapped[list["SaleReturnPayment"]] = relationship(
        back_populates="sale_return",
        lazy="selectin",
    )


class SaleReturnLine(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "sale_return_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    sale_return_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sale_returns.id", ondelete="CASCADE"),
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
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
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

    sale_return: Mapped["SaleReturn"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    original_sale_line: Mapped["SaleLine | None"] = relationship(
        foreign_keys=[sale_line_id],
        lazy="selectin",
    )
    product: Mapped["Product"] = relationship(lazy="selectin")
    variation: Mapped["ProductVariation | None"] = relationship(lazy="selectin")


class SaleReturnPayment(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "sale_return_payments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    sale_return_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sale_returns.id", ondelete="CASCADE"),
        nullable=False,
    )
    payment_method: Mapped[str] = mapped_column(
        payment_method_enum, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        payment_status_enum, nullable=False, default="completed"
    )
    refunded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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

    sale_return: Mapped["SaleReturn"] = relationship(
        back_populates="payments",
        lazy="selectin",
    )
