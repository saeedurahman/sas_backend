import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin
from app.models.enums import (
    price_list_type_enum,
    product_type_enum,
    sync_status_enum,
    tracking_type_enum,
)

if TYPE_CHECKING:
    from app.models.business import Branch


class Category(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    parent: Mapped["Category | None"] = relationship(
        back_populates="children",
        remote_side=[id],
        foreign_keys=[parent_id],
        lazy="selectin",
    )
    children: Mapped[list["Category"]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_id],
        lazy="selectin",
    )
    products: Mapped[list["Product"]] = relationship(
        back_populates="category",
        lazy="selectin",
    )


class Brand(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "brands"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="brand",
        lazy="selectin",
    )


class Unit(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "units"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    is_base_unit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="base_unit",
        lazy="selectin",
    )
    variations: Mapped[list["ProductVariation"]] = relationship(
        back_populates="unit",
        lazy="selectin",
    )


class UnitConversion(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "unit_conversions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_unit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("units.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_unit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("units.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversion_factor: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    from_unit: Mapped["Unit"] = relationship(
        foreign_keys=[from_unit_id],
        lazy="selectin",
    )
    to_unit: Mapped["Unit"] = relationship(
        foreign_keys=[to_unit_id],
        lazy="selectin",
    )


class Product(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("brands.id", ondelete="SET NULL"),
        nullable=True,
    )
    base_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("units.id", ondelete="RESTRICT"),
        nullable=True,
    )
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    product_type: Mapped[str] = mapped_column(product_type_enum, nullable=False)
    tracking_type: Mapped[str] = mapped_column(tracking_type_enum, nullable=False)
    is_sellable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_purchasable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    shelf_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_stock_level: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    max_stock_level: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    category: Mapped["Category | None"] = relationship(
        back_populates="products",
        lazy="selectin",
    )
    brand: Mapped["Brand | None"] = relationship(
        back_populates="products",
        lazy="selectin",
    )
    base_unit: Mapped["Unit | None"] = relationship(
        back_populates="products",
        lazy="selectin",
    )
    variations: Mapped[list["ProductVariation"]] = relationship(
        back_populates="product",
        lazy="selectin",
    )
    barcodes: Mapped[list["Barcode"]] = relationship(
        back_populates="product",
        lazy="selectin",
    )
    locations: Mapped[list["ProductLocation"]] = relationship(
        back_populates="product",
        lazy="selectin",
    )
    price_list_items: Mapped[list["PriceListItem"]] = relationship(
        back_populates="product",
        lazy="selectin",
    )


class ProductVariation(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "product_variations"

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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("units.id", ondelete="RESTRICT"),
        nullable=True,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weight_grams: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    product: Mapped["Product"] = relationship(
        back_populates="variations",
        lazy="selectin",
    )
    unit: Mapped["Unit | None"] = relationship(
        back_populates="variations",
        lazy="selectin",
    )
    barcodes: Mapped[list["Barcode"]] = relationship(
        back_populates="variation",
        lazy="selectin",
    )
    locations: Mapped[list["ProductLocation"]] = relationship(
        back_populates="variation",
        lazy="selectin",
    )
    price_list_items: Mapped[list["PriceListItem"]] = relationship(
        back_populates="variation",
        lazy="selectin",
    )


class ProductLocation(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "product_locations"

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
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_stock_level: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    max_stock_level: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    bin_location: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    branch: Mapped["Branch"] = relationship(lazy="selectin")
    product: Mapped["Product"] = relationship(
        back_populates="locations",
        lazy="selectin",
    )
    variation: Mapped["ProductVariation | None"] = relationship(
        back_populates="locations",
        lazy="selectin",
    )


class Barcode(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "barcodes"

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
    barcode: Mapped[str] = mapped_column(String(100), nullable=False)
    barcode_type: Mapped[str] = mapped_column(String(20), nullable=False, default="EAN13")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    product: Mapped["Product"] = relationship(
        back_populates="barcodes",
        lazy="selectin",
    )
    variation: Mapped["ProductVariation | None"] = relationship(
        back_populates="barcodes",
        lazy="selectin",
    )


class PriceList(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "price_lists"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    list_type: Mapped[str] = mapped_column(price_list_type_enum, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    items: Mapped[list["PriceListItem"]] = relationship(
        back_populates="price_list",
        lazy="selectin",
    )


class PriceListItem(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "price_list_items"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    price_list_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("price_lists.id", ondelete="CASCADE"),
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
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    min_qty: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False, default=1)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    price_list: Mapped["PriceList"] = relationship(
        back_populates="items",
        lazy="selectin",
    )
    product: Mapped["Product"] = relationship(
        back_populates="price_list_items",
        lazy="selectin",
    )
    variation: Mapped["ProductVariation | None"] = relationship(
        back_populates="price_list_items",
        lazy="selectin",
    )
