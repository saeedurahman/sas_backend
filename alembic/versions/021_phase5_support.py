"""Phase 5: customer price lists + public receipt links.

* ``customers.price_list_id`` (FK price_lists, ON DELETE SET NULL). The 019 change_seq
  trigger on customers is ``UPDATE OF <columns fixed at migration time>``, so it is rebuilt
  here to include the new column - otherwise assigning a list would never reach devices.
* ``receipt_links``: revocable, expiring public receipt links (the URL carries only a random
  id plus an HMAC; everything else lives here).

Revision ID: 021_phase5_support
Revises: 020_default_price_list
Create Date: 2026-11-13

"""

from typing import Sequence, Union

from alembic import op

revision: str = "021_phase5_support"
down_revision: Union[str, Sequence[str], None] = "020_default_price_list"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IGNORED_COLUMNS = {
    "change_seq", "sync_status", "local_id", "server_id",
    "created_at", "created_by", "updated_by", "deleted_by",
}


def _rebuild_customer_trigger(conn) -> None:
    rows = conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'customers' "
        "ORDER BY ordinal_position"
    ).fetchall()
    cols = [r[0] for r in rows if r[0] not in _IGNORED_COLUMNS and r[0] != "id"]
    op.execute("DROP TRIGGER IF EXISTS trg_customers_sync_stamp ON customers")
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_customers_sync_stamp "
        f"AFTER INSERT OR UPDATE OF {', '.join(cols)} ON customers "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION sync_stamp()"
    )


def upgrade() -> None:
    conn = op.get_bind()

    op.execute(
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS price_list_id UUID "
        "REFERENCES price_lists(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_customers_price_list ON customers (price_list_id) "
        "WHERE price_list_id IS NOT NULL"
    )
    _rebuild_customer_trigger(conn)

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_links (
            id UUID PRIMARY KEY,
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            sale_id UUID NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            created_by UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            revoked_by UUID
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_receipt_links_sale ON receipt_links (business_id, sale_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS receipt_links")
    op.execute("DROP INDEX IF EXISTS ix_customers_price_list")
    op.execute("DROP TRIGGER IF EXISTS trg_customers_sync_stamp ON customers")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS price_list_id")
    conn = op.get_bind()
    _rebuild_customer_trigger(conn)
