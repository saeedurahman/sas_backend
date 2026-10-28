import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DocumentNumberCounter(Base):
    __tablename__ = "document_number_counters"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "prefix",
            "date_key",
            name="uq_document_number_counters_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    prefix: Mapped[str] = mapped_column(String(10), nullable=False)
    date_key: Mapped[str] = mapped_column(String(8), nullable=False)
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
