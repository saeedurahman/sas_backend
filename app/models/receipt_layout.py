import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin


class ReceiptLayout(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    """A receipt/invoice layout: business data (validated JSON), not code."""

    __tablename__ = "receipt_layouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    design: Mapped[str] = mapped_column(String(30), nullable=False)
    paper: Mapped[str] = mapped_column(String(10), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default="synced")


class ReceiptLayoutAssignment(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    """Which layout a channel uses; ``branch_id`` NULL = the business default."""

    __tablename__ = "receipt_layout_assignments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    layout_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("receipt_layouts.id", ondelete="CASCADE"), nullable=False
    )
    sync_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default="synced")


class SaleLineBatch(Base):
    """The ACTUAL batch allocation of a sale line (what the server consumed)."""

    __tablename__ = "sale_line_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    sale_line_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sale_lines.id", ondelete="CASCADE"), nullable=False
    )
    layer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("purchase_lines.id", ondelete="SET NULL"), nullable=True
    )
    batch_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
