import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin, utc_now
from app.models.enums import (
    payment_method_enum,
    reference_type_enum,
    register_tx_type_enum,
    shift_status_enum,
    sync_status_enum,
)

if TYPE_CHECKING:
    from app.models.business import Branch, Business
    from app.models.user import User


class CashRegister(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "cash_registers"

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
    device_identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch"] = relationship(lazy="selectin")
    shifts: Mapped[list["RegisterShift"]] = relationship(
        back_populates="cash_register",
        lazy="selectin",
    )


class RegisterShift(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "register_shifts"

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
    cash_register_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cash_registers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    opened_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        shift_status_enum, nullable=False, default="open"
    )
    opening_float: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    expected_cash: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    actual_cash: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    cash_difference: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    closed_at: Mapped[datetime | None] = mapped_column(
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
    cash_register: Mapped["CashRegister"] = relationship(
        back_populates="shifts",
        lazy="selectin",
    )
    opened_by_user: Mapped["User"] = relationship(
        foreign_keys=[opened_by],
        lazy="selectin",
    )
    closed_by_user: Mapped["User | None"] = relationship(
        foreign_keys=[closed_by],
        lazy="selectin",
    )
    transactions: Mapped[list["RegisterTransaction"]] = relationship(
        back_populates="shift",
        lazy="selectin",
    )


class RegisterTransaction(Base, SoftDeleteMixin, SyncMixin):
    __tablename__ = "register_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    register_shift_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("register_shifts.id", ondelete="CASCADE"),
        nullable=False,
    )
    tx_type: Mapped[str] = mapped_column(register_tx_type_enum, nullable=False)
    payment_method: Mapped[str] = mapped_column(
        payment_method_enum, nullable=False, default="cash"
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(
        reference_type_enum, nullable=True
    )
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    transacted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
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

    business: Mapped["Business"] = relationship(lazy="selectin")
    shift: Mapped["RegisterShift"] = relationship(
        back_populates="transactions",
        lazy="selectin",
    )
