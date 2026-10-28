"""Security hardening: refresh-token families, view permissions, phone uniqueness.

- refresh_tokens.family_id (rotation + reuse detection)
- roles.view / permissions.view / branches.view permissions, backfilled to roles
- one active user per phone (only created when no duplicates exist yet)

Revision ID: 015_security_hardening
Revises: 014_accounting_permissions
Create Date: 2026-07-01

"""

from typing import Sequence, Union

from alembic import op

from app.services.role_permission_seed import backfill_role_permissions_for_all_businesses
from migration_utils import SEEDS_DIR, run_sql_file

revision: str = "015_security_hardening"
down_revision: Union[str, Sequence[str], None] = "014_accounting_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_KEYS = ("roles.view", "permissions.view", "branches.view")


def upgrade() -> None:
    op.execute("ALTER TABLE refresh_tokens ADD COLUMN IF NOT EXISTS family_id UUID")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_family "
        "ON refresh_tokens (user_id, family_id) WHERE revoked_at IS NULL"
    )

    # A phone identifies one active account. Skipped (with a notice) if legacy
    # duplicates exist; application checks still apply and login rejects
    # ambiguous phones with 409 rather than guessing.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM users
                WHERE deleted_at IS NULL AND phone IS NOT NULL
                GROUP BY phone HAVING COUNT(*) > 1
            ) THEN
                RAISE NOTICE 'uq_users_phone_active skipped: duplicate phones exist';
            ELSE
                CREATE UNIQUE INDEX IF NOT EXISTS uq_users_phone_active
                    ON users (phone) WHERE deleted_at IS NULL;
            END IF;
        END $$;
        """
    )

    run_sql_file(op.get_bind(), SEEDS_DIR / "007_security_view_permissions.sql")
    backfill_role_permissions_for_all_businesses(op.get_bind(), dry_run=False)


def downgrade() -> None:
    keys_sql = ", ".join(f"'{key}'" for key in _NEW_KEYS)
    op.execute(
        f"""
        DELETE FROM role_permissions rp
        USING permissions p
        WHERE rp.permission_id = p.id
          AND p.permission_key IN ({keys_sql})
        """
    )
    op.execute(f"DELETE FROM permissions WHERE permission_key IN ({keys_sql})")
    op.execute("DROP INDEX IF EXISTS uq_users_phone_active")
    op.execute("DROP INDEX IF EXISTS idx_refresh_tokens_family")
    op.execute("ALTER TABLE refresh_tokens DROP COLUMN IF EXISTS family_id")
