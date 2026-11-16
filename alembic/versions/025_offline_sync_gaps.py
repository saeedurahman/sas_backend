"""Phase 6c: offline sync gaps.

- Add change_seq to modifier_groups, modifiers, product_modifier_groups,
  floor_plans, tables, register_shifts, sales.
- Add soft-delete and sync columns to product_modifier_groups.
- Replace uq_product_modifier_groups with a partial index (WHERE deleted_at IS NULL).
- Backfill change_seq for existing rows in these tables.
- Attach sync_stamp triggers on all 7 tables.
- Update trg_businesses_settings_sync to include contact, address, tax_id.
- Add trg_app_settings_sync to touch business_configs on receipt/tax settings change.

Revision ID: 025_offline_sync_gaps
Revises: 024_platform_hardening
Create Date: 2026-11-16
"""

from typing import Sequence, Union
from alembic import op

revision: str = "025_offline_sync_gaps"
down_revision: Union[str, Sequence[str], None] = "024_platform_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_SYNCED_TABLES = [
    "modifier_groups",
    "modifiers",
    "product_modifier_groups",
    "floor_plans",
    "tables",
    "register_shifts",
    "sales",
]

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


def _data_columns(conn, table: str) -> list[str]:
    rows = conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows if r[0] not in _IGNORED_COLUMNS and r[0] != "id"]


def _backfill_sql(table: str) -> str:
    order_col = "created_at, id" if table != "product_modifier_groups" else "id"
    return f"""
    WITH numbered AS (
        SELECT id, business_id,
               row_number() OVER (PARTITION BY business_id ORDER BY {order_col}) AS rn
          FROM {table}
         WHERE change_seq IS NULL
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
     WHERE c.business_id = u.business_id;
    """


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Schema adjustments on product_modifier_groups
    op.execute(
        """
        ALTER TABLE product_modifier_groups
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS local_id UUID,
            ADD COLUMN IF NOT EXISTS server_id UUID,
            ADD COLUMN IF NOT EXISTS sync_status VARCHAR(20) DEFAULT 'synced';
        """
    )
    op.execute("ALTER TABLE product_modifier_groups DROP CONSTRAINT IF EXISTS uq_product_modifier_groups;")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_product_modifier_groups
            ON product_modifier_groups (business_id, product_id, modifier_group_id)
            WHERE deleted_at IS NULL;
        """
    )

    # 2. Add change_seq column & index for all new synced tables
    for table in NEW_SYNCED_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS change_seq BIGINT;")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_change_seq ON {table} (business_id, change_seq) "
            f"WHERE change_seq IS NOT NULL;"
        )

    # 3. Backfill change_seq on existing rows
    for table in NEW_SYNCED_TABLES:
        op.execute(_backfill_sql(table))

    # 4. Attach deferred change_seq stamp triggers
    for table in NEW_SYNCED_TABLES:
        cols = _data_columns(conn, table)
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_stamp ON {table};")
        op.execute(
            f"CREATE CONSTRAINT TRIGGER trg_{table}_sync_stamp "
            f"AFTER INSERT OR UPDATE OF {', '.join(cols)} ON {table} "
            f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_stamp();"
        )

    # 5. Rebuild businesses sync touch trigger to include branding & contact info
    op.execute("DROP TRIGGER IF EXISTS trg_businesses_settings_sync ON businesses;")
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_businesses_settings_sync
        AFTER UPDATE OF name, legal_name, phone, tax_id, address_line1, address_line2, city,
                        postal_code, currency_code, timezone, is_active, subscription_status,
                        subscription_ends_at, trial_ends_at, deleted_at ON businesses
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_touch_settings_of_business();
        """
    )

    # 6. Add trigger on app_settings to touch business_configs so settings feed updates
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_touch_settings_of_app_setting() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_business UUID;
        BEGIN
            v_business := CASE WHEN TG_OP = 'DELETE' THEN OLD.business_id ELSE NEW.business_id END;
            UPDATE business_configs SET change_seq = sync_next_seq(business_id)
             WHERE business_id = v_business;
            RETURN NULL;
        END $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_app_settings_sync ON app_settings;")
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_app_settings_sync
        AFTER INSERT OR UPDATE OF setting_value, deleted_at OR DELETE ON app_settings
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_touch_settings_of_app_setting();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_app_settings_sync ON app_settings;")
    op.execute("DROP FUNCTION IF EXISTS sync_touch_settings_of_app_setting();")

    for table in NEW_SYNCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_stamp ON {table};")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_change_seq;")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS change_seq;")

    op.execute("DROP INDEX IF EXISTS uq_product_modifier_groups;")
    op.execute(
        "ALTER TABLE product_modifier_groups ADD CONSTRAINT uq_product_modifier_groups "
        "UNIQUE (business_id, product_id, modifier_group_id);"
    )
    op.execute(
        """
        ALTER TABLE product_modifier_groups
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS deleted_by,
            DROP COLUMN IF EXISTS local_id,
            DROP COLUMN IF EXISTS server_id,
            DROP COLUMN IF EXISTS sync_status;
        """
    )
