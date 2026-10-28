"""Atomic per-business document number counters.

Revision ID: 010_document_number_counters
Revises: 009_stock_movement_sequence
Create Date: 2026-06-29

"""

from typing import Sequence, Union

from alembic import op

revision: str = "010_document_number_counters"
down_revision: Union[str, Sequence[str], None] = "009_stock_movement_sequence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BACKFILL_SOURCES: list[tuple[str, str, str]] = [
    ("sales", "sale_number", "INV"),
    ("sale_returns", "return_number", "RET"),
    ("purchase_orders", "po_number", "PO"),
    ("purchase_receipts", "receipt_number", "GRN"),
    ("stock_adjustments", "adjustment_number", "ADJ"),
    ("stock_transfers", "transfer_number", "TRF"),
    ("waste_entries", "waste_number", "WST"),
    ("expenses", "expense_number", "EXP"),
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE document_number_counters (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id     UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            prefix          VARCHAR(10) NOT NULL,
            date_key        VARCHAR(8) NOT NULL,
            last_sequence   INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT uq_document_number_counters_scope
                UNIQUE (business_id, prefix, date_key)
        );

        CREATE INDEX idx_document_number_counters_business
            ON document_number_counters (business_id);

        COMMENT ON TABLE document_number_counters IS
            'Atomic daily sequence counters for generate_document_number().';
        """
    )

    for table, column, prefix in _BACKFILL_SOURCES:
        op.execute(
            f"""
            INSERT INTO document_number_counters (
                id, business_id, prefix, date_key, last_sequence
            )
            SELECT
                gen_random_uuid(),
                business_id,
                '{prefix}',
                split_part({column}, '-', 2),
                MAX(CAST(split_part({column}, '-', 3) AS INTEGER))
            FROM {table}
            WHERE {column} ~ '^{prefix}-[0-9]{{8}}-[0-9]+$'
              AND deleted_at IS NULL
            GROUP BY business_id, split_part({column}, '-', 2)
            ON CONFLICT (business_id, prefix, date_key)
            DO UPDATE SET last_sequence = GREATEST(
                document_number_counters.last_sequence,
                EXCLUDED.last_sequence
            );
            """
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_number_counters;")
