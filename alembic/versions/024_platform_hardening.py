"""Platform admin hardening and branch scoping.

- Platform admin TOTP secrets encrypted with Fernet (widened to VARCHAR(255)).
- Platform admin recovery codes hashes column (JSONB).
- Platform revoked tokens blacklist table (platform_revoked_tokens).
- Customer ledger and supplier ledger branch_id foreign key columns and composite indexes.
- Audit action enum includes 'export'.
- Existing platform admin TOTP secrets encrypted in place.

Revision ID: 024_platform_hardening
Revises: 023_performance_indexes
Create Date: 2026-11-16
"""

from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

from app.core.platform_security import (
    encrypt_totp_secret,
    decrypt_totp_secret,
    is_fernet_token,
)

revision: str = "024_platform_hardening"
down_revision: Union[str, Sequence[str], None] = "023_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Audit action enum 'export'
    op.execute("ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'export'")

    # 2. Platform admins: alter totp_secret length to 255, add recovery_codes_hashes
    op.alter_column(
        "platform_admins",
        "totp_secret",
        type_=sa.String(255),
        existing_type=sa.String(64),
        existing_nullable=False,
    )
    op.add_column(
        "platform_admins",
        sa.Column(
            "recovery_codes_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
        ),
    )

    # 3. Create platform_revoked_tokens table
    op.create_table(
        "platform_revoked_tokens",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column(
            "admin_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_admins.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_platform_revoked_tokens_expires_at",
        "platform_revoked_tokens",
        ["expires_at"],
    )

    # 4. Add branch_id to customer_ledger & supplier_ledger
    op.add_column(
        "customer_ledger",
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_customer_ledger_business_branch_date",
        "customer_ledger",
        ["business_id", "branch_id", "entry_at"],
    )

    op.add_column(
        "supplier_ledger",
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_supplier_ledger_business_branch_date",
        "supplier_ledger",
        ["business_id", "branch_id", "entry_at"],
    )

    # 5. Encrypt existing plain TOTP secrets in place
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, totp_secret FROM platform_admins WHERE totp_secret IS NOT NULL")
    ).fetchall()
    for row in rows:
        secret = row.totp_secret
        if secret and not is_fernet_token(secret):
            enc = encrypt_totp_secret(secret)
            conn.execute(
                sa.text("UPDATE platform_admins SET totp_secret = :enc WHERE id = :id"),
                {"enc": enc, "id": row.id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    # Decrypt TOTP secrets back if fernet
    rows = conn.execute(
        sa.text("SELECT id, totp_secret FROM platform_admins WHERE totp_secret IS NOT NULL")
    ).fetchall()
    for row in rows:
        secret = row.totp_secret
        if secret and is_fernet_token(secret):
            plain = decrypt_totp_secret(secret)
            conn.execute(
                sa.text("UPDATE platform_admins SET totp_secret = :plain WHERE id = :id"),
                {"plain": plain[:64], "id": row.id},
            )

    op.drop_index("idx_supplier_ledger_business_branch_date", table_name="supplier_ledger")
    op.drop_column("supplier_ledger", "branch_id")

    op.drop_index("idx_customer_ledger_business_branch_date", table_name="customer_ledger")
    op.drop_column("customer_ledger", "branch_id")

    op.drop_index("idx_platform_revoked_tokens_expires_at", table_name="platform_revoked_tokens")
    op.drop_table("platform_revoked_tokens")

    op.drop_column("platform_admins", "recovery_codes_hashes")
    op.alter_column(
        "platform_admins",
        "totp_secret",
        type_=sa.String(64),
        existing_type=sa.String(255),
        existing_nullable=False,
    )
