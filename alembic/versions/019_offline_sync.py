"""Offline-first sync: change feed, device registry, op log, offline sale flags.

* ``change_seq`` (per-business, gap-free-ish, commit-ordered) on every synced table.
  Rows are stamped at COMMIT by a deferred constraint trigger that takes the business's
  counter row lock last, so sequence order == commit order and a pull cursor can never
  skip a row that commits late.
* ``stock_levels`` - derived per product+branch balance maintained by a trigger on
  ``stock_movements`` (stock itself stays a ledger; this is only the sync view of it).
* ``sync_counters`` / ``sync_devices`` / ``sync_ops``.
* sales: client receipt number, device, sync flags, offline marker.
* audit_logs.device_id.
* Backfill: every existing synced row gets a change_seq.

Revision ID: 019_offline_sync
Revises: 018_subscription_platform
Create Date: 2026-10-29

"""

from typing import Sequence, Union

from alembic import op

revision: str = "019_offline_sync"
down_revision: Union[str, Sequence[str], None] = "018_subscription_platform"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# table -> columns whose change should produce a new change_seq (None = every data column).
# users/roles only expose a few fields to devices, so logins etc. must not churn the feed.
SYNCED_TABLES: dict[str, tuple[str, ...] | None] = {
    "categories": None,
    "brands": None,
    "units": None,
    "tax_rates": None,
    "products": None,
    "product_variations": None,
    "barcodes": None,
    "price_lists": None,
    "price_list_items": None,
    "customers": None,
    "discount_schemes": None,
    "branches": None,
    "cash_registers": None,
    "business_configs": None,
    "users": ("full_name", "default_branch_id", "status", "is_locked", "deleted_at"),
    "roles": ("name", "description", "is_system", "deleted_at"),
    "stock_levels": ("qty", "last_movement_at", "deleted_at"),
}

# Bookkeeping columns that never count as a data change.
_IGNORED_COLUMNS = {
    "change_seq",
    "sync_status",
    "local_id",
    "server_id",
    "created_at",
    "created_by",
    "updated_by",
    "deleted_by",
}

ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# Backfill runs before the triggers exist, so it never fires them.
BACKFILL_ORDER = [
    "branches", "business_configs", "cash_registers", "categories", "brands", "units",
    "tax_rates", "products", "product_variations", "barcodes", "price_lists",
    "price_list_items", "customers", "discount_schemes", "roles", "users", "stock_levels",
]


def _backfill_sql(table: str) -> str:
    return f"""
    WITH numbered AS (
        SELECT id, business_id,
               row_number() OVER (PARTITION BY business_id ORDER BY created_at, id) AS rn
          FROM {table}
    ), upd AS (
        UPDATE {table} x
           SET change_seq = c.last_seq + n.rn
          FROM numbered n JOIN sync_counters c ON c.business_id = n.business_id
         WHERE x.id = n.id
        RETURNING x.business_id
    )
    UPDATE sync_counters c
       SET last_seq = c.last_seq + u.cnt
      FROM (SELECT business_id, count(*) AS cnt FROM upd GROUP BY business_id) u
     WHERE c.business_id = u.business_id
    """


def _data_columns(conn, table: str) -> list[str]:
    rows = conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows if r[0] not in _IGNORED_COLUMNS and r[0] != "id"]


def upgrade() -> None:
    conn = op.get_bind()

    # ── new tables ────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE sync_counters (
            business_id UUID PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
            last_seq BIGINT NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE stock_levels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            branch_id UUID NOT NULL,
            product_id UUID NOT NULL,
            variation_id UUID,
            variation_key UUID NOT NULL DEFAULT '{ZERO_UUID}',
            qty NUMERIC(15,4) NOT NULL DEFAULT 0,
            last_movement_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            change_seq BIGINT,
            CONSTRAINT uq_stock_levels_position
                UNIQUE (business_id, branch_id, product_id, variation_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sync_devices (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            device_id VARCHAR(100) NOT NULL,
            name VARCHAR(150),
            app_version VARCHAR(50),
            last_user_id UUID,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at TIMESTAMPTZ,
            revoked_by UUID,
            CONSTRAINT uq_sync_devices_device UNIQUE (business_id, device_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sync_ops (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            op_id UUID NOT NULL,
            device_id VARCHAR(100) NOT NULL,
            batch_id VARCHAR(100),
            user_id UUID,
            op_type VARCHAR(40) NOT NULL,
            status VARCHAR(12) NOT NULL,
            payload_hash CHAR(64) NOT NULL,
            result JSONB NOT NULL,
            client_created_at TIMESTAMPTZ,
            occurred_at TIMESTAMPTZ,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_sync_ops_op UNIQUE (business_id, op_id),
            CONSTRAINT ck_sync_ops_status CHECK (status IN ('applied', 'rejected', 'conflict'))
        )
        """
    )
    op.execute("CREATE INDEX ix_sync_ops_device ON sync_ops (business_id, device_id, received_at)")

    # ── columns on existing tables ────────────────────────────────────────────
    op.execute(
        """
        ALTER TABLE sales
            ADD COLUMN client_receipt_no VARCHAR(64),
            ADD COLUMN device_id VARCHAR(100),
            ADD COLUMN sync_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN is_offline BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN client_created_at TIMESTAMPTZ,
            ADD COLUMN synced_at TIMESTAMPTZ
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sales_client_receipt ON sales (business_id, client_receipt_no) "
        "WHERE client_receipt_no IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_sales_sync_flagged ON sales (business_id, synced_at DESC) "
        "WHERE sync_flags <> '[]'::jsonb"
    )
    op.execute("ALTER TABLE audit_logs ADD COLUMN device_id VARCHAR(100)")

    for table in SYNCED_TABLES:
        if table != "stock_levels":
            op.execute(f"ALTER TABLE {table} ADD COLUMN change_seq BIGINT")
        op.execute(
            f"CREATE INDEX ix_{table}_change_seq ON {table} (business_id, change_seq) "
            "WHERE change_seq IS NOT NULL"
        )

    # ── backfill (before any trigger exists) ──────────────────────────────────
    op.execute("INSERT INTO sync_counters (business_id, last_seq) SELECT id, 0 FROM businesses")
    op.execute(
        f"""
        INSERT INTO stock_levels (business_id, branch_id, product_id, variation_id,
                                  variation_key, qty, last_movement_at)
        SELECT business_id, branch_id, product_id, variation_id,
               COALESCE(variation_id, '{ZERO_UUID}'::uuid),
               SUM(qty), MAX(movement_at)
          FROM stock_movements
         WHERE deleted_at IS NULL
         GROUP BY business_id, branch_id, product_id, variation_id
        """
    )
    for table in BACKFILL_ORDER:
        op.execute(_backfill_sql(table))

    # ── functions ─────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE FUNCTION sync_next_seq(p_business UUID) RETURNS BIGINT
        LANGUAGE sql AS $$
            INSERT INTO sync_counters (business_id, last_seq) VALUES (p_business, 1)
            ON CONFLICT (business_id) DO UPDATE SET last_seq = sync_counters.last_seq + 1
            RETURNING last_seq
        $$
        """
    )
    # Deferred: runs at COMMIT. The counter row lock is therefore the last lock a
    # transaction takes and is held only until its commit completes, which makes
    # change_seq order identical to commit order for a business.
    op.execute(
        """
        CREATE FUNCTION sync_stamp() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_business UUID;
            v_id UUID;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_business := OLD.business_id; v_id := OLD.id;
            ELSE
                v_business := NEW.business_id; v_id := NEW.id;
            END IF;
            EXECUTE format(
                'UPDATE %I.%I SET change_seq = sync_next_seq($1) WHERE id = $2',
                TG_TABLE_SCHEMA, TG_TABLE_NAME
            ) USING v_business, v_id;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_touch(p_table TEXT, p_id UUID) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            EXECUTE format(
                'UPDATE %I SET change_seq = sync_next_seq(business_id) WHERE id = $1', p_table
            ) USING p_id;
        END $$
        """
    )
    # Link tables bump the synced row that embeds them.
    op.execute(
        """
        CREATE FUNCTION sync_touch_user_of_role_link() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN PERFORM sync_touch('users', OLD.user_id);
            ELSE
                PERFORM sync_touch('users', NEW.user_id);
                IF TG_OP = 'UPDATE' AND OLD.user_id <> NEW.user_id THEN
                    PERFORM sync_touch('users', OLD.user_id);
                END IF;
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_touch_role_perm() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE v_role UUID;
        BEGIN
            v_role := CASE WHEN TG_OP = 'DELETE' THEN OLD.role_id ELSE NEW.role_id END;
            PERFORM sync_touch('roles', v_role);
            PERFORM sync_touch('users', ur.user_id)
               FROM user_roles ur WHERE ur.role_id = v_role AND ur.deleted_at IS NULL;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_touch_users_of_role() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM sync_touch('users', ur.user_id)
               FROM user_roles ur WHERE ur.role_id = NEW.id AND ur.deleted_at IS NULL;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_touch_customer_of_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM sync_touch('customers', NEW.customer_id);
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_touch_settings_of_business() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE business_configs SET change_seq = sync_next_seq(business_id)
             WHERE business_id = NEW.id;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION sync_stock_levels() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            old_q NUMERIC := 0;
            new_q NUMERIC := 0;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.deleted_at IS NULL THEN old_q := OLD.qty; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.deleted_at IS NULL THEN new_q := NEW.qty; END IF;

            IF TG_OP = 'UPDATE'
               AND OLD.business_id = NEW.business_id AND OLD.branch_id = NEW.branch_id
               AND OLD.product_id = NEW.product_id
               AND OLD.variation_id IS NOT DISTINCT FROM NEW.variation_id THEN
                IF new_q <> old_q THEN
                    PERFORM sync_apply_stock(NEW.business_id, NEW.branch_id, NEW.product_id,
                                             NEW.variation_id, new_q - old_q, NEW.movement_at);
                END IF;
                RETURN NULL;
            END IF;

            IF TG_OP IN ('UPDATE', 'DELETE') AND old_q <> 0 THEN
                PERFORM sync_apply_stock(OLD.business_id, OLD.branch_id, OLD.product_id,
                                         OLD.variation_id, -old_q, NULL);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND new_q <> 0 THEN
                PERFORM sync_apply_stock(NEW.business_id, NEW.branch_id, NEW.product_id,
                                         NEW.variation_id, new_q, NEW.movement_at);
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION sync_apply_stock(
            p_business UUID, p_branch UUID, p_product UUID, p_variation UUID,
            p_delta NUMERIC, p_at TIMESTAMPTZ
        ) RETURNS void LANGUAGE sql AS $$
            INSERT INTO stock_levels (business_id, branch_id, product_id, variation_id,
                                      variation_key, qty, last_movement_at)
            VALUES (p_business, p_branch, p_product, p_variation,
                    COALESCE(p_variation, '{ZERO_UUID}'::uuid), p_delta, p_at)
            ON CONFLICT (business_id, branch_id, product_id, variation_key) DO UPDATE
               SET qty = stock_levels.qty + EXCLUDED.qty,
                   last_movement_at = GREATEST(stock_levels.last_movement_at,
                                               EXCLUDED.last_movement_at),
                   updated_at = now()
        $$
        """
    )

    # ── triggers ──────────────────────────────────────────────────────────────
    op.execute(
        "CREATE TRIGGER trg_stock_levels_from_movements "
        "AFTER INSERT OR UPDATE OR DELETE ON stock_movements "
        "FOR EACH ROW EXECUTE FUNCTION sync_stock_levels()"
    )
    for table, columns in SYNCED_TABLES.items():
        cols = list(columns) if columns is not None else _data_columns(conn, table)
        op.execute(
            f"CREATE CONSTRAINT TRIGGER trg_{table}_sync_stamp "
            f"AFTER INSERT OR UPDATE OF {', '.join(cols)} ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_stamp()"
        )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_user_roles_sync "
        "AFTER INSERT OR DELETE OR UPDATE OF user_id, role_id, branch_id, deleted_at ON user_roles "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_touch_user_of_role_link()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_role_permissions_sync "
        "AFTER INSERT OR DELETE OR UPDATE ON role_permissions "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_touch_role_perm()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_roles_users_sync "
        "AFTER UPDATE OF name, deleted_at ON roles "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_touch_users_of_role()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_customer_ledger_sync "
        "AFTER INSERT OR UPDATE OF amount, deleted_at ON customer_ledger "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_touch_customer_of_ledger()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_businesses_settings_sync "
        "AFTER UPDATE OF name, currency_code, timezone, is_active, subscription_status, "
        "subscription_ends_at, trial_ends_at, deleted_at ON businesses "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_touch_settings_of_business()"
    )


def downgrade() -> None:
    for table in SYNCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_stamp ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_user_roles_sync ON user_roles")
    op.execute("DROP TRIGGER IF EXISTS trg_role_permissions_sync ON role_permissions")
    op.execute("DROP TRIGGER IF EXISTS trg_roles_users_sync ON roles")
    op.execute("DROP TRIGGER IF EXISTS trg_customer_ledger_sync ON customer_ledger")
    op.execute("DROP TRIGGER IF EXISTS trg_businesses_settings_sync ON businesses")
    op.execute("DROP TRIGGER IF EXISTS trg_stock_levels_from_movements ON stock_movements")
    for fn in (
        "sync_stamp()", "sync_touch(TEXT, UUID)", "sync_next_seq(UUID)",
        "sync_touch_user_of_role_link()", "sync_touch_role_perm()", "sync_touch_users_of_role()",
        "sync_touch_customer_of_ledger()", "sync_touch_settings_of_business()",
        "sync_stock_levels()",
        "sync_apply_stock(UUID, UUID, UUID, UUID, NUMERIC, TIMESTAMPTZ)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    op.execute("ALTER TABLE audit_logs DROP COLUMN IF EXISTS device_id")
    op.execute("DROP INDEX IF EXISTS ix_sales_sync_flagged")
    op.execute("DROP INDEX IF EXISTS uq_sales_client_receipt")
    op.execute(
        "ALTER TABLE sales DROP COLUMN IF EXISTS client_receipt_no, DROP COLUMN IF EXISTS device_id, "
        "DROP COLUMN IF EXISTS sync_flags, DROP COLUMN IF EXISTS is_offline, "
        "DROP COLUMN IF EXISTS client_created_at, DROP COLUMN IF EXISTS synced_at"
    )
    for table in SYNCED_TABLES:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_change_seq")
        if table != "stock_levels":
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS change_seq")
    op.execute("DROP TABLE IF EXISTS sync_ops")
    op.execute("DROP TABLE IF EXISTS sync_devices")
    op.execute("DROP TABLE IF EXISTS stock_levels")
    op.execute("DROP TABLE IF EXISTS sync_counters")
