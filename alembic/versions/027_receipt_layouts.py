"""Receipt layouts, device short codes, numbering, pharmacy batch/expiry.

- receipt_layouts / receipt_layout_assignments (synced, change_seq stamped at commit).
- Seeds four layouts per business (one per design) and assigns thermal_classic as the
  business default for print, whatsapp and link; pharmacies get batch/expiry printing on.
- sync_devices.short_code (C1, C2, ... in registration order, unique per business).
- sales.sale_seq: numeric counter behind the official number (sorting without text order).
- units.allows_decimal, products.track_expiry, products.name_secondary.
- purchase_lines: change_seq + stamp trigger so batch layers feed the ``stock_batch`` entity.
- sale_line_batches: the actual batch allocation of every sale line.

Idempotent: every step checks for existing objects first.

Revision ID: 027_receipt_layouts
Revises: 026_tax_price_settings
Create Date: 2026-11-17
"""

import json
from typing import Sequence, Union

from alembic import op

revision: str = "027_receipt_layouts"
down_revision: Union[str, Sequence[str], None] = "026_tax_price_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IGNORED = {
    "change_seq", "sync_status", "local_id", "server_id", "created_at", "created_by",
    "updated_by", "deleted_by",
}
_WEIGHT_VOLUME_SYMBOLS = (
    "kg", "g", "gm", "gram", "mg", "l", "ltr", "lt", "ml", "m", "cm", "mm", "ft", "yd", "lb", "oz",
    "meter", "metre", "dozen", "yard",
)


def _data_columns(conn, table: str) -> list[str]:
    rows = conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows if r[0] not in _IGNORED and r[0] != "id"]


def _stamp_trigger(conn, table: str, *, cols: list[str] | None = None, when: str | None = None) -> None:
    cols = cols or _data_columns(conn, table)
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_stamp ON {table}")
    clause = f" WHEN ({when})" if when else ""
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_{table}_sync_stamp "
        f"AFTER INSERT OR UPDATE OF {', '.join(cols)} ON {table} "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW{clause} EXECUTE FUNCTION sync_stamp()"
    )


def _backfill(table: str, where: str = "TRUE") -> str:
    return f"""
    WITH numbered AS (
        SELECT id, business_id,
               row_number() OVER (PARTITION BY business_id ORDER BY created_at, id) AS rn
          FROM {table} WHERE change_seq IS NULL AND {where}
    ), upd AS (
        UPDATE {table} x SET change_seq = COALESCE(c.last_seq, 0) + n.rn
          FROM numbered n LEFT JOIN sync_counters c ON c.business_id = n.business_id
         WHERE x.id = n.id
        RETURNING x.business_id
    )
    INSERT INTO sync_counters (business_id, last_seq)
    SELECT business_id, count(*) FROM upd GROUP BY business_id
    ON CONFLICT (business_id) DO UPDATE SET last_seq = sync_counters.last_seq + EXCLUDED.last_seq;
    """


def upgrade() -> None:
    conn = op.get_bind()

    # ── layouts ──────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_layouts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            design VARCHAR(30) NOT NULL
                CHECK (design IN ('thermal_classic','thermal_compact','a4_tax_invoice','a4_elegant')),
            paper VARCHAR(10) NOT NULL CHECK (paper IN ('roll58','roll80','a4')),
            config JSONB NOT NULL DEFAULT '{"v": 1}'::jsonb,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID,
            updated_by UUID,
            deleted_at TIMESTAMPTZ,
            deleted_by UUID,
            sync_status VARCHAR(20) DEFAULT 'synced',
            local_id UUID,
            server_id UUID,
            change_seq BIGINT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_receipt_layouts_business ON receipt_layouts (business_id) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_receipt_layouts_change_seq ON receipt_layouts "
        "(business_id, change_seq) WHERE change_seq IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_layout_assignments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            branch_id UUID REFERENCES branches(id) ON DELETE CASCADE,
            channel VARCHAR(10) NOT NULL CHECK (channel IN ('print','whatsapp','link')),
            layout_id UUID NOT NULL REFERENCES receipt_layouts(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID,
            updated_by UUID,
            deleted_at TIMESTAMPTZ,
            deleted_by UUID,
            sync_status VARCHAR(20) DEFAULT 'synced',
            local_id UUID,
            server_id UUID,
            change_seq BIGINT
        )
        """
    )
    # One assignment per scope; a NULL branch is the business default (one more scope).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_layout_assignment_scope "
        "ON receipt_layout_assignments "
        "(business_id, COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid), channel) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_receipt_layout_assignments_change_seq "
        "ON receipt_layout_assignments (business_id, change_seq) WHERE change_seq IS NOT NULL"
    )

    # ── device short code ────────────────────────────────────────────────────
    op.execute("ALTER TABLE sync_devices ADD COLUMN IF NOT EXISTS short_code VARCHAR(4)")
    op.execute(
        """
        WITH numbered AS (
            SELECT id, row_number() OVER (PARTITION BY business_id ORDER BY first_seen_at, id) AS rn
              FROM sync_devices WHERE short_code IS NULL
        )
        UPDATE sync_devices d SET short_code = 'C' || (
                 n.rn + COALESCE((SELECT count(*) FROM sync_devices x
                                   WHERE x.business_id = d.business_id AND x.short_code IS NOT NULL), 0)
               )
          FROM numbered n WHERE d.id = n.id
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_devices_short_code "
        "ON sync_devices (business_id, upper(short_code)) WHERE short_code IS NOT NULL"
    )

    # ── official number: numeric counter ─────────────────────────────────────
    op.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS sale_seq BIGINT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sales_sale_seq ON sales (business_id, sale_seq) "
        "WHERE sale_seq IS NOT NULL"
    )

    # ── catalog columns ──────────────────────────────────────────────────────
    op.execute("ALTER TABLE units ADD COLUMN IF NOT EXISTS allows_decimal BOOLEAN NOT NULL DEFAULT FALSE")
    symbols = ", ".join(f"'{s}'" for s in _WEIGHT_VOLUME_SYMBOLS)
    op.execute(
        f"UPDATE units SET allows_decimal = TRUE WHERE allows_decimal = FALSE "
        f"AND (lower(symbol) IN ({symbols}) OR lower(name) IN ('kilogram','gram','litre','liter','meter','metre'))"
    )
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS track_expiry BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS name_secondary VARCHAR(255)")
    _stamp_trigger(conn, "units")
    _stamp_trigger(conn, "products")

    # ── batch layers ─────────────────────────────────────────────────────────
    op.execute("ALTER TABLE purchase_lines ADD COLUMN IF NOT EXISTS change_seq BIGINT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_purchase_lines_change_seq ON purchase_lines "
        "(business_id, change_seq) WHERE change_seq IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_purchase_lines_fefo ON purchase_lines "
        "(business_id, branch_id, product_id, expiry_date NULLS LAST, created_at) "
        "WHERE deleted_at IS NULL AND qty_remaining > 0"
    )
    op.execute(
        _backfill(
            "purchase_lines",
            "(batch_number IS NOT NULL OR expiry_date IS NOT NULL) AND qty_remaining > 0 "
            "AND deleted_at IS NULL",
        )
    )
    _stamp_trigger(
        conn,
        "purchase_lines",
        cols=[
            "qty_remaining", "batch_number", "expiry_date", "deleted_at", "branch_id",
            "product_id", "variation_id", "received_qty",
        ],
        when="NEW.batch_number IS NOT NULL OR NEW.expiry_date IS NOT NULL",
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sale_line_batches (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            sale_line_id UUID NOT NULL REFERENCES sale_lines(id) ON DELETE CASCADE,
            layer_id UUID REFERENCES purchase_lines(id) ON DELETE SET NULL,
            batch_no VARCHAR(100),
            expiry_date DATE,
            qty NUMERIC(15,4) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sale_line_batches_line ON sale_line_batches (sale_line_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sale_line_batches_layer ON sale_line_batches (layer_id)"
    )

    # ── seed layouts for existing businesses (never touches a business that has some) ──
    _seed_existing_businesses(conn)

    # ── layout stamp triggers + backfill (after seeding, so seeded rows get a sequence) ──
    for table in ("receipt_layouts", "receipt_layout_assignments"):
        op.execute(_backfill(table))
        _stamp_trigger(conn, table)


def _seed_existing_businesses(conn) -> None:
    from app.services.receipt_layout_config import DESIGN_NAMES, DESIGN_PAPER, DESIGNS, default_config

    rows = conn.exec_driver_sql(
        """
        SELECT b.id, COALESCE(t.code, '') AS code
          FROM businesses b LEFT JOIN business_types t ON t.id = b.business_type_id
         WHERE b.deleted_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM receipt_layouts l WHERE l.business_id = b.id)
        """
    ).fetchall()
    for business_id, code in rows:
        pharmacy = code == "pharmacy"
        classic_id = None
        for design in DESIGNS:
            layout_id = conn.exec_driver_sql(
                "INSERT INTO receipt_layouts (business_id, name, design, paper, config) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
                (
                    business_id, DESIGN_NAMES[design], design, DESIGN_PAPER[design],
                    json.dumps(default_config(design, pharmacy=pharmacy)),
                ),
            ).scalar()
            if design == "thermal_classic":
                classic_id = layout_id
        for channel in ("print", "whatsapp", "link"):
            conn.exec_driver_sql(
                "INSERT INTO receipt_layout_assignments (business_id, branch_id, channel, layout_id) "
                "VALUES (%s, NULL, %s, %s)",
                (business_id, channel, classic_id),
            )


def downgrade() -> None:
    for table in ("receipt_layout_assignments", "receipt_layouts", "sale_line_batches"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_purchase_lines_sync_stamp ON purchase_lines")
    op.execute("DROP INDEX IF EXISTS ix_purchase_lines_fefo")
    op.execute("DROP INDEX IF EXISTS ix_purchase_lines_change_seq")
    op.execute("ALTER TABLE purchase_lines DROP COLUMN IF EXISTS change_seq")
    op.execute("DROP INDEX IF EXISTS ix_sales_sale_seq")
    op.execute("ALTER TABLE sales DROP COLUMN IF EXISTS sale_seq")
    op.execute("DROP INDEX IF EXISTS uq_sync_devices_short_code")
    op.execute("ALTER TABLE sync_devices DROP COLUMN IF EXISTS short_code")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS track_expiry")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS name_secondary")
    op.execute("ALTER TABLE units DROP COLUMN IF EXISTS allows_decimal")
