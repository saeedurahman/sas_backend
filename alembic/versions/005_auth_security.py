"""Auth security: user lockout columns and refresh_tokens table.

Revision ID: 005_auth_security
Revises: 004_business_types_lookup
Create Date: 2026-06-02

"""

from typing import Sequence, Union

from alembic import op

revision: str = "005_auth_security"
down_revision: Union[str, Sequence[str], None] = "004_business_types_lookup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. users: pin_code → pin_hash (skip if already pin_hash)
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'users'
                  AND column_name = 'pin_code'
            ) THEN
                ALTER TABLE users RENAME COLUMN pin_code TO pin_hash;
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS is_locked BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS failed_login_attempts SMALLINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
        """
    )

    op.execute(
        """
        COMMENT ON COLUMN users.pin_hash IS
            'bcrypt hash for quick POS PIN — never store plain text';
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_users_locked
            ON users (business_id, is_locked)
            WHERE is_locked = TRUE;
        """
    )

    # 2. refresh_tokens
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            jti         VARCHAR(255) UNIQUE NOT NULL,
            expires_at  TIMESTAMPTZ NOT NULL,
            revoked_at  TIMESTAMPTZ,
            ip_address  VARCHAR(45),
            user_agent  TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id
            ON refresh_tokens (user_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_refresh_tokens_jti
            ON refresh_tokens (jti);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_refresh_tokens_active
            ON refresh_tokens (user_id, expires_at)
            WHERE revoked_at IS NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_refresh_tokens_business_id
            ON refresh_tokens (business_id);
        """
    )

    # Tenant RLS (table may be created after 012_utilities ran)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename = 'refresh_tokens' AND policyname = 'tenant_isolation'
            ) THEN
                ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation ON refresh_tokens
                    USING (business_id = current_business_id())
                    WITH CHECK (business_id = current_business_id());
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS tenant_isolation ON refresh_tokens;
        ALTER TABLE refresh_tokens DISABLE ROW LEVEL SECURITY;
        """
    )
    op.execute("DROP TABLE IF EXISTS refresh_tokens CASCADE;")

    op.execute("DROP INDEX IF EXISTS idx_users_locked;")

    op.execute(
        """
        ALTER TABLE users
            DROP COLUMN IF EXISTS password_changed_at,
            DROP COLUMN IF EXISTS locked_until,
            DROP COLUMN IF EXISTS failed_login_attempts,
            DROP COLUMN IF EXISTS is_locked;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'users'
                  AND column_name = 'pin_hash'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'users'
                  AND column_name = 'pin_code'
            ) THEN
                ALTER TABLE users RENAME COLUMN pin_hash TO pin_code;
            END IF;
        END $$;
        """
    )
