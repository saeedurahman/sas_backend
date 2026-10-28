"""Replace businesses.business_type enum with business_types lookup table.

Revision ID: 004_business_types_lookup
Revises: 003_fifo_functions
Create Date: 2026-06-02

"""

from typing import Sequence, Union

from alembic import op

from migration_utils import SEEDS_DIR, run_sql_file

revision: str = "004_business_types_lookup"
down_revision: Union[str, Sequence[str], None] = "003_fifo_functions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Lookup table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS business_types (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code        VARCHAR(50) UNIQUE NOT NULL,
            name        VARCHAR(100) NOT NULL,
            description TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order  SMALLINT DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_types_code
            ON business_types (code) WHERE is_active = TRUE;
        """
    )

    run_sql_file(conn, SEEDS_DIR / "002_business_types.sql")

    # 2. Add FK column (nullable during backfill)
    op.execute(
        """
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS business_type_id UUID
            REFERENCES business_types(id) ON DELETE RESTRICT;
        """
    )

    # 3. Map legacy enum values to lookup codes (incl. mixed → other)
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'businesses' AND column_name = 'business_type'
            ) THEN
                UPDATE businesses b
                SET business_type_id = bt.id
                FROM business_types bt
                WHERE b.business_type_id IS NULL
                  AND bt.code = CASE b.business_type::text
                      WHEN 'mixed' THEN 'other'
                      ELSE b.business_type::text
                  END;
            END IF;
        END $$;
        """
    )

    # 4. Fallback any unmapped rows to retail
    op.execute(
        """
        UPDATE businesses b
        SET business_type_id = bt.id
        FROM business_types bt
        WHERE b.business_type_id IS NULL
          AND bt.code = 'retail';
        """
    )

    op.execute(
        """
        ALTER TABLE businesses
            ALTER COLUMN business_type_id SET NOT NULL;
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_businesses_business_type_id ON businesses (business_type_id);")

    # 5. Drop legacy enum column
    op.execute(
        """
        ALTER TABLE businesses DROP COLUMN IF EXISTS business_type;
        """
    )

    # 6. Drop enum type if nothing else references it
    op.execute("DROP TYPE IF EXISTS business_type_enum;")


def downgrade() -> None:
    op.execute(
        """
        CREATE TYPE business_type_enum AS ENUM (
            'bakery', 'restaurant', 'mart', 'retail', 'mixed'
        );
        """
    )
    op.execute(
        """
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS business_type business_type_enum;
        """
    )
    op.execute(
        """
        UPDATE businesses b
        SET business_type = bt.code::business_type_enum
        FROM business_types bt
        WHERE b.business_type_id = bt.id
          AND bt.code IN ('bakery', 'restaurant', 'mart', 'retail');
        """
    )
    op.execute(
        """
        UPDATE businesses SET business_type = 'retail'::business_type_enum
        WHERE business_type IS NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE businesses
            ALTER COLUMN business_type SET NOT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE businesses DROP CONSTRAINT IF EXISTS businesses_business_type_id_fkey;
        ALTER TABLE businesses DROP COLUMN IF EXISTS business_type_id;
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_businesses_business_type_id;")
    op.execute("DROP TABLE IF EXISTS business_types CASCADE;")
