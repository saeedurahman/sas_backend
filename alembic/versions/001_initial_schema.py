"""Initial database schema (all SQL groups + utilities + FIFO function).

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-06-02

"""

from typing import Sequence, Union

from alembic import op

from migration_utils import INITIAL_SCHEMA_FILES, run_schema_files

revision: str = "001_initial_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    # Exclude 013 if applied separately — included in INITIAL_SCHEMA_FILES for greenfield
    files = [f for f in INITIAL_SCHEMA_FILES if f != "013_fifo_functions.sql"]
    run_schema_files(connection, files)


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS get_fifo_cost(UUID, UUID, UUID, NUMERIC);
        DROP MATERIALIZED VIEW IF EXISTS mv_stock_balances;
        DROP VIEW IF EXISTS v_fifo_layers;
        """
    )
    op.execute(
        """
        DO $$ DECLARE r RECORD;
        BEGIN
            FOR r IN (
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename NOT IN ('alembic_version')
            ) LOOP
                EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', r.tablename);
            END LOOP;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ DECLARE t TEXT;
        BEGIN
            FOR t IN (
                SELECT typname FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'public' AND t.typtype = 'e'
            ) LOOP
                EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', t);
            END LOOP;
        END $$;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS current_business_id() CASCADE")
