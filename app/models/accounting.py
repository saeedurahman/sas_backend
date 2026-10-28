import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuditMixin, Base, SoftDeleteMixin, SyncMixin, TimestampMixin
from app.models.enums import (
    account_subtype_enum,
    account_type_enum,
    journal_entry_status_enum,
    reference_type_enum,
    sync_status_enum,
)

if TYPE_CHECKING:
    from app.models.business import Branch, Business


class ChartOfAccount(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "chart_of_accounts"
    __table_args__ = (
        UniqueConstraint("business_id", "account_code", name="uq_coa_code"),
    )

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
        ForeignKey("chart_of_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_code: Mapped[str] = mapped_column(String(20), nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(account_type_enum, nullable=False)
    account_subtype: Mapped[str | None] = mapped_column(
        account_subtype_enum,
        nullable=True,
    )
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum, nullable=False, default="synced"
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    parent: Mapped["ChartOfAccount | None"] = relationship(
        back_populates="children",
        remote_side=[id],
        foreign_keys=[parent_id],
        lazy="selectin",
    )
    children: Mapped[list["ChartOfAccount"]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_id],
        lazy="selectin",
    )
    journal_lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="account",
        lazy="selectin",
    )


class JournalEntry(Base, AuditMixin, SoftDeleteMixin, SyncMixin):
    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("business_id", "entry_number", name="uq_journal_entries_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("branches.id", ondelete="SET NULL"),
        nullable=True,
    )
    entry_number: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        journal_entry_status_enum,
        nullable=False,
        default="draft",
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_type: Mapped[str | None] = mapped_column(
        reference_type_enum,
        nullable=True,
    )
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    sync_status: Mapped[str] = mapped_column(
        sync_status_enum,
        nullable=False,
        default="pending",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    business: Mapped["Business"] = relationship(lazy="selectin")
    branch: Mapped["Branch | None"] = relationship(lazy="selectin")
    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="journal_entry",
        lazy="selectin",
        order_by="JournalLine.line_order",
    )


class JournalLine(Base, TimestampMixin, SyncMixin):
    __tablename__ = "journal_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    debit_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    credit_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=0
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    journal_entry: Mapped["JournalEntry"] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    account: Mapped["ChartOfAccount"] = relationship(
        back_populates="journal_lines",
        lazy="selectin",
    )
