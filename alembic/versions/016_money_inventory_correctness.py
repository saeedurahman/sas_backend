"""Phase 2: money and inventory correctness.

- users.token_version (access tokens invalidated on password/PIN change)
- purchase_lines.branch_id / layer_source: FIFO layers are per branch; legacy
  rows are backfilled and untracked stock gets a synthetic layer
- unit costs widened to NUMERIC(15,4); tax rates to NUMERIC(6,3)
- sale_lines.cost_estimated; sale_payments.tendered_amount / change_given
- idempotency_keys table
- one open shift per register and per opener (partial unique indexes)
- CHECK constraints (added NOT VALID, then validated when legacy data allows)
- get_fifo_cost() is branch-aware, ordered by (created_at, id), 4 decimals
- sales.void / sales.override_tax / shifts.manage permissions

Revision ID: 016_money_inventory_correctness
Revises: 015_security_hardening
Create Date: 2026-10-28

"""

from typing import Sequence, Union

from alembic import op

from app.services.role_permission_seed import backfill_role_permissions_for_all_businesses

revision: str = "016_money_inventory_correctness"
down_revision: Union[str, Sequence[str], None] = "015_security_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COST_COLUMNS = (
    "purchase_lines",
    "purchase_receipt_lines",
    "stock_movements",
    "stock_adjustment_lines",
    "stock_transfer_lines",
    "waste_entry_lines",
    "production_lines",
    "sale_lines",
)

# (table, constraint, expression)
_CHECKS = (
    ("sale_lines", "chk_sale_lines_tax_rate", "tax_rate >= 0 AND tax_rate <= 100"),
    ("sale_lines", "chk_sale_lines_tax_amount", "tax_amount >= 0"),
    ("sale_lines", "chk_sale_lines_discount_amount", "discount_amount >= 0"),
    ("sale_lines", "chk_sale_lines_cost", "cost_per_unit >= 0"),
    ("purchase_lines", "chk_purchase_lines_tax_rate", "tax_rate >= 0 AND tax_rate <= 100"),
    ("sale_payments", "chk_sale_payments_change", "change_given >= 0"),
    ("sale_return_lines", "chk_sale_return_lines_price", "unit_price >= 0"),
    ("sale_return_lines", "chk_sale_return_lines_tax", "tax_amount >= 0"),
    ("stock_movements", "chk_stock_movements_cost", "cost_per_unit >= 0"),
    ("register_shifts", "chk_register_shifts_float", "opening_float >= 0"),
)

_NEW_PERMISSIONS = (
    ("sales.void", "sales", "Void Sales — Void completed sales on an open shift"),
    ("sales.override_tax", "sales", "Override Tax — Supply a tax rate at point of sale"),
    ("shifts.manage", "register", "Manage Shifts — Close shifts opened by other users"),
)


def _add_check(table: str, name: str, expr: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
                ALTER TABLE {table}
                    ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID;
            END IF;
            BEGIN
                ALTER TABLE {table} VALIDATE CONSTRAINT {name};
            EXCEPTION WHEN check_violation THEN
                RAISE NOTICE '{name} left NOT VALID: legacy rows violate it';
            END;
        END $$;
        """
    )


def upgrade() -> None:
    # ── users ────────────────────────────────────────────────────────────────
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0"
    )

    # ── dependent views must go before the column type changes ──────────────
    op.execute("DROP VIEW IF EXISTS v_fifo_layers")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_stock_balances")

    # ── purchase_lines: branch + source ─────────────────────────────────────
    op.execute(
        """
        ALTER TABLE purchase_lines
            ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id) ON DELETE RESTRICT,
            ADD COLUMN IF NOT EXISTS layer_source VARCHAR(20) NOT NULL DEFAULT 'purchase';

        -- Where the stock actually landed (receipt movement) wins over the PO's branch.
        UPDATE purchase_lines pl
           SET branch_id = m.branch_id
          FROM (
                SELECT DISTINCT ON (purchase_line_id) purchase_line_id, branch_id
                  FROM stock_movements
                 WHERE purchase_line_id IS NOT NULL
                   AND movement_type IN ('purchase', 'production_in')
                 ORDER BY purchase_line_id, movement_at
               ) m
         WHERE pl.id = m.purchase_line_id AND pl.branch_id IS NULL;

        UPDATE purchase_lines pl
           SET branch_id = po.branch_id
          FROM purchase_orders po
         WHERE pl.purchase_order_id = po.id AND pl.branch_id IS NULL;

        UPDATE purchase_lines pl
           SET branch_id = pr.branch_id, layer_source = 'production'
          FROM production_orders pr
         WHERE pl.production_order_id = pr.id AND pl.branch_id IS NULL;

        UPDATE purchase_lines SET layer_source = 'production'
         WHERE production_order_id IS NOT NULL AND layer_source = 'purchase';

        CREATE INDEX IF NOT EXISTS idx_purchase_lines_fifo_branch
            ON purchase_lines (business_id, branch_id, product_id, variation_id, created_at, id)
            WHERE deleted_at IS NULL AND qty_remaining > 0;
        """
    )

    # ── precision ────────────────────────────────────────────────────────────
    for table in _COST_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN cost_per_unit TYPE NUMERIC(15,4)"
        )
    op.execute("ALTER TABLE sale_lines ALTER COLUMN tax_rate TYPE NUMERIC(6,3)")
    op.execute("ALTER TABLE purchase_lines ALTER COLUMN tax_rate TYPE NUMERIC(6,3)")
    op.execute("ALTER TABLE tax_rates ALTER COLUMN rate TYPE NUMERIC(6,3)")

    # ── sales: estimated cost, cash change ──────────────────────────────────
    op.execute(
        """
        ALTER TABLE sale_lines
            ADD COLUMN IF NOT EXISTS cost_estimated BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE sale_payments
            ADD COLUMN IF NOT EXISTS tendered_amount NUMERIC(15,2),
            ADD COLUMN IF NOT EXISTS change_given NUMERIC(15,2) NOT NULL DEFAULT 0;
        """
    )

    # ── synthetic layers for stock that never had one (legacy adjustments / transfers) ──
    op.execute(
        """
        INSERT INTO purchase_lines (
            id, business_id, purchase_order_id, production_order_id, product_id,
            variation_id, branch_id, layer_source, ordered_qty, received_qty,
            qty_remaining, cost_per_unit, tax_rate, created_at, updated_at, created_by
        )
        SELECT gen_random_uuid(), b.business_id, NULL, NULL, b.product_id,
               b.variation_id, b.branch_id, 'adjustment', b.diff, b.diff, b.diff,
               b.cost, 0, b.first_at, b.first_at, b.created_by
          FROM (
                SELECT sb.business_id, sb.branch_id, sb.product_id, sb.variation_id,
                       sb.bal - COALESCE(l.rem, 0) AS diff,
                       sb.first_at,
                       COALESCE((
                           SELECT sm.cost_per_unit FROM stock_movements sm
                            WHERE sm.business_id = sb.business_id
                              AND sm.branch_id = sb.branch_id
                              AND sm.product_id = sb.product_id
                              AND sm.variation_id IS NOT DISTINCT FROM sb.variation_id
                              AND sm.deleted_at IS NULL AND sm.qty > 0
                            ORDER BY sm.movement_at DESC LIMIT 1
                       ), 0) AS cost,
                       sb.created_by
                  FROM (
                        SELECT business_id, branch_id, product_id, variation_id,
                               SUM(qty) AS bal, MIN(movement_at) AS first_at,
                               (ARRAY_AGG(created_by ORDER BY movement_at))[1] AS created_by
                          FROM stock_movements
                         WHERE deleted_at IS NULL
                         GROUP BY business_id, branch_id, product_id, variation_id
                       ) sb
                  LEFT JOIN (
                        SELECT business_id, branch_id, product_id, variation_id,
                               SUM(qty_remaining) AS rem
                          FROM purchase_lines
                         WHERE deleted_at IS NULL AND branch_id IS NOT NULL
                         GROUP BY business_id, branch_id, product_id, variation_id
                       ) l
                    ON l.business_id = sb.business_id
                   AND l.branch_id = sb.branch_id
                   AND l.product_id = sb.product_id
                   AND l.variation_id IS NOT DISTINCT FROM sb.variation_id
               ) b
         WHERE b.diff > 0
        """
    )

    # ── views (recreated with the widened columns, branch from the layer) ───
    op.execute(
        """
        CREATE MATERIALIZED VIEW mv_stock_balances AS
        SELECT
            sm.business_id,
            sm.branch_id,
            sm.product_id,
            sm.variation_id,
            SUM(sm.qty)                    AS on_hand_qty,
            SUM(sm.qty * sm.cost_per_unit) AS inventory_value,
            MAX(sm.movement_at)            AS last_movement_at
        FROM stock_movements sm
        WHERE sm.deleted_at IS NULL
        GROUP BY sm.business_id, sm.branch_id, sm.product_id, sm.variation_id;

        CREATE UNIQUE INDEX idx_mv_stock_balances_pk
            ON mv_stock_balances (business_id, branch_id, product_id, variation_id);
        CREATE INDEX idx_mv_stock_balances_business
            ON mv_stock_balances (business_id, branch_id);

        CREATE VIEW v_fifo_layers AS
        SELECT
            pl.business_id,
            pl.branch_id,
            pl.product_id,
            pl.variation_id,
            pl.id            AS purchase_line_id,
            pl.cost_per_unit,
            pl.qty_remaining,
            pl.received_qty,
            pl.created_at    AS layer_date,
            pl.batch_number,
            pl.expiry_date
        FROM purchase_lines pl
        WHERE pl.deleted_at IS NULL AND pl.qty_remaining > 0;
        """
    )

    # ── idempotency ──────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id     UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            user_id         UUID,
            key             VARCHAR(128) NOT NULL,
            method          VARCHAR(10) NOT NULL,
            path            VARCHAR(255) NOT NULL,
            request_hash    VARCHAR(64) NOT NULL,
            response_status INTEGER,
            response_body   JSONB,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at      TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_idempotency_business_key UNIQUE (business_id, key)
        );
        CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_keys (expires_at);
        """
    )

    # ── shifts: one open per register, one open per opener ───────────────────
    for name, column in (
        ("uq_register_shifts_open_register", "cash_register_id"),
        ("uq_register_shifts_open_opener", "opened_by"),
    ):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM register_shifts
                     WHERE status = 'open' AND deleted_at IS NULL
                     GROUP BY {column} HAVING COUNT(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        '{name}: duplicate open shifts exist on {column}; close extras first';
                END IF;
                CREATE UNIQUE INDEX IF NOT EXISTS {name}
                    ON register_shifts ({column})
                 WHERE status = 'open' AND deleted_at IS NULL;
            END $$;
            """
        )

    # ── CHECK constraints ────────────────────────────────────────────────────
    for table, name, expr in _CHECKS:
        _add_check(table, name, expr)

    # ── FIFO cost function: branch-aware, (created_at, id), 4 decimals ──────
    op.execute("DROP FUNCTION IF EXISTS get_fifo_cost(UUID, UUID, UUID, NUMERIC)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION get_fifo_cost(
            p_business_id  UUID,
            p_branch_id    UUID,
            p_product_id   UUID,
            p_variation_id UUID,
            p_qty          NUMERIC
        )
        RETURNS NUMERIC(15,4)
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            v_remaining  NUMERIC := p_qty;
            v_total_cost NUMERIC := 0;
            v_take       NUMERIC;
            r            RECORD;
        BEGIN
            IF p_qty IS NULL OR p_qty <= 0 THEN
                RAISE EXCEPTION 'get_fifo_cost: p_qty must be positive (got %)', p_qty;
            END IF;

            FOR r IN
                SELECT pl.cost_per_unit, pl.qty_remaining
                  FROM purchase_lines pl
                 WHERE pl.business_id = p_business_id
                   AND (p_branch_id IS NULL OR pl.branch_id = p_branch_id)
                   AND pl.product_id = p_product_id
                   AND pl.variation_id IS NOT DISTINCT FROM p_variation_id
                   AND pl.qty_remaining > 0
                   AND pl.deleted_at IS NULL
                 ORDER BY pl.created_at ASC, pl.id ASC
            LOOP
                v_take := LEAST(v_remaining, r.qty_remaining);
                v_total_cost := v_total_cost + (v_take * r.cost_per_unit);
                v_remaining := v_remaining - v_take;
                EXIT WHEN v_remaining <= 0;
            END LOOP;

            IF v_remaining > 0 THEN
                RAISE EXCEPTION
                    'get_fifo_cost: insufficient FIFO stock for business=%, branch=%, product=%, variation=% — need %, short by %',
                    p_business_id, p_branch_id, p_product_id, p_variation_id, p_qty, v_remaining;
            END IF;

            RETURN ROUND(v_total_cost / p_qty, 4);
        END;
        $$;

        COMMENT ON FUNCTION get_fifo_cost IS
            'Weighted-average unit cost (4 dp) from the oldest layers at a branch (NULL = all branches).';
        """
    )

    # ── permissions ──────────────────────────────────────────────────────────
    for key, module, description in _NEW_PERMISSIONS:
        op.execute(
            f"""
            INSERT INTO permissions (id, permission_key, module, description)
            VALUES (gen_random_uuid(), '{key}', '{module}', '{description}')
            ON CONFLICT (permission_key) DO NOTHING
            """
        )
    backfill_role_permissions_for_all_businesses(op.get_bind(), dry_run=False)


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k, _, _ in _NEW_PERMISSIONS)
    op.execute(
        f"""
        DELETE FROM role_permissions rp USING permissions p
         WHERE rp.permission_id = p.id AND p.permission_key IN ({keys})
        """
    )
    op.execute(f"DELETE FROM permissions WHERE permission_key IN ({keys})")

    op.execute("DROP FUNCTION IF EXISTS get_fifo_cost(UUID, UUID, UUID, UUID, NUMERIC)")
    for table, name, _ in _CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS uq_register_shifts_open_opener")
    op.execute("DROP INDEX IF EXISTS uq_register_shifts_open_register")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")

    op.execute("DROP VIEW IF EXISTS v_fifo_layers")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_stock_balances")
    op.execute(
        """
        ALTER TABLE sale_payments
            DROP COLUMN IF EXISTS change_given,
            DROP COLUMN IF EXISTS tendered_amount;
        ALTER TABLE sale_lines DROP COLUMN IF EXISTS cost_estimated;
        ALTER TABLE tax_rates ALTER COLUMN rate TYPE NUMERIC(5,2);
        ALTER TABLE purchase_lines ALTER COLUMN tax_rate TYPE NUMERIC(5,2);
        ALTER TABLE sale_lines ALTER COLUMN tax_rate TYPE NUMERIC(5,2);
        """
    )
    for table in _COST_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN cost_per_unit TYPE NUMERIC(15,2)")
    op.execute(
        """
        DROP INDEX IF EXISTS idx_purchase_lines_fifo_branch;
        DELETE FROM purchase_lines WHERE layer_source = 'adjustment' AND purchase_order_id IS NULL;
        ALTER TABLE purchase_lines
            DROP COLUMN IF EXISTS layer_source,
            DROP COLUMN IF EXISTS branch_id;

        CREATE MATERIALIZED VIEW mv_stock_balances AS
        SELECT sm.business_id, sm.branch_id, sm.product_id, sm.variation_id,
               SUM(sm.qty) AS on_hand_qty,
               SUM(sm.qty * sm.cost_per_unit) AS inventory_value,
               MAX(sm.movement_at) AS last_movement_at
          FROM stock_movements sm
         WHERE sm.deleted_at IS NULL
         GROUP BY sm.business_id, sm.branch_id, sm.product_id, sm.variation_id;
        CREATE UNIQUE INDEX idx_mv_stock_balances_pk
            ON mv_stock_balances (business_id, branch_id, product_id, variation_id);
        CREATE INDEX idx_mv_stock_balances_business
            ON mv_stock_balances (business_id, branch_id);
        CREATE VIEW v_fifo_layers AS
        SELECT pl.business_id, po.branch_id, pl.product_id, pl.variation_id,
               pl.id AS purchase_line_id, pl.cost_per_unit, pl.qty_remaining,
               pl.received_qty, pl.created_at AS layer_date, pl.batch_number, pl.expiry_date
          FROM purchase_lines pl
          JOIN purchase_orders po ON po.id = pl.purchase_order_id
         WHERE pl.deleted_at IS NULL AND pl.qty_remaining > 0;
        """
    )
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS token_version")
