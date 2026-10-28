"""Subscription enforcement, platform admin, license issuing, branch on audit rows.

* plans / subscription_payments / platform_settings / platform_audit_log /
  platform_admins / platform_business_notes / licenses
* businesses.plan_id; audit_logs.branch_id
* existing businesses become ``active`` with a year of subscription (dev data is not
  locked out); default plans Basic and Multi-branch (placeholder prices)

Revision ID: 018_subscription_platform
Revises: 017_return_line_discount
Create Date: 2026-10-29

"""

from typing import Sequence, Union

from alembic import op

revision: str = "018_subscription_platform"
down_revision: Union[str, Sequence[str], None] = "017_return_line_discount"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Existing businesses: do not lock out current data. Public so tests can replay it.
BACKFILL_SQL = """
UPDATE businesses
   SET subscription_status = 'active',
       subscription_ends_at = NOW() + INTERVAL '365 days',
       plan_id = COALESCE(plan_id, (SELECT id FROM plans WHERE name = 'Basic'))
 WHERE subscription_status IN ('trial', 'active', 'past_due')
   AND deleted_at IS NULL;
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_admins (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email VARCHAR(255) NOT NULL UNIQUE,
            name VARCHAR(120) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            totp_secret VARCHAR(64) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            token_version INTEGER NOT NULL DEFAULT 0,
            failed_login_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TIMESTAMPTZ,
            last_totp_step BIGINT NOT NULL DEFAULT 0,
            last_login TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_platform_admins_email_lower CHECK (email = LOWER(email))
        );

        CREATE TABLE IF NOT EXISTS plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name VARCHAR(100) NOT NULL UNIQUE,
            monthly_price_pkr NUMERIC(12,2) NOT NULL DEFAULT 0,
            onetime_price_pkr NUMERIC(12,2) NOT NULL DEFAULT 0,
            notes TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_plans_prices CHECK (monthly_price_pkr >= 0 AND onetime_price_pkr >= 0)
        );

        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES plans(id) ON DELETE SET NULL;
        CREATE INDEX IF NOT EXISTS idx_businesses_plan_id ON businesses (plan_id);
        CREATE INDEX IF NOT EXISTS idx_businesses_subscription
            ON businesses (subscription_status, subscription_ends_at);

        CREATE TABLE IF NOT EXISTS subscription_payments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            plan_id UUID REFERENCES plans(id) ON DELETE SET NULL,
            amount_pkr NUMERIC(12,2) NOT NULL,
            method VARCHAR(20) NOT NULL,
            reference VARCHAR(120),
            note TEXT,
            period_months INTEGER NOT NULL,
            period_start TIMESTAMPTZ NOT NULL,
            period_end TIMESTAMPTZ NOT NULL,
            recorded_by UUID REFERENCES platform_admins(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_subscription_payments_amount CHECK (amount_pkr > 0),
            CONSTRAINT chk_subscription_payments_months CHECK (period_months BETWEEN 1 AND 36),
            CONSTRAINT chk_subscription_payments_method
                CHECK (method IN ('jazzcash', 'easypaisa', 'bank', 'cash'))
        );
        CREATE INDEX IF NOT EXISTS idx_subscription_payments_business
            ON subscription_payments (business_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_subscription_payments_created
            ON subscription_payments (created_at);
        -- The same transaction id recorded twice for one business is a double submit.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_subscription_payments_reference
            ON subscription_payments (business_id, method, reference)
            WHERE reference IS NOT NULL;

        CREATE TABLE IF NOT EXISTS platform_settings (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by UUID REFERENCES platform_admins(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS platform_audit_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            admin_id UUID REFERENCES platform_admins(id) ON DELETE SET NULL,
            admin_email VARCHAR(255) NOT NULL,
            action VARCHAR(80) NOT NULL,
            entity_type VARCHAR(50) NOT NULL,
            entity_id VARCHAR(64),
            entity_label VARCHAR(255),
            business_id UUID,
            before JSONB,
            after JSONB,
            ip_address VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_platform_audit_created
            ON platform_audit_log (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_platform_audit_business
            ON platform_audit_log (business_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS platform_business_notes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            admin_id UUID REFERENCES platform_admins(id) ON DELETE SET NULL,
            admin_email VARCHAR(255) NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_platform_business_notes
            ON platform_business_notes (business_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS licenses (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            license_id VARCHAR(40) NOT NULL UNIQUE,
            customer_name VARCHAR(200) NOT NULL,
            phone VARCHAR(50),
            email VARCHAR(255),
            edition VARCHAR(20) NOT NULL DEFAULT 'local',
            machine_id VARCHAR(200) NOT NULL,
            issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updates_until DATE NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT 'issued',
            transfer_count INTEGER NOT NULL DEFAULT 0,
            previous_license_id UUID REFERENCES licenses(id) ON DELETE SET NULL,
            revoked_at TIMESTAMPTZ,
            notes TEXT,
            key_string TEXT NOT NULL,
            created_by UUID REFERENCES platform_admins(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_licenses_status CHECK (status IN ('issued', 'active', 'revoked')),
            CONSTRAINT chk_licenses_transfer_count CHECK (transfer_count >= 0)
        );
        CREATE INDEX IF NOT EXISTS idx_licenses_machine ON licenses (machine_id);
        CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses (status);

        -- Tenant audit rows record the branch the action happened in.
        ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS branch_id UUID;
        CREATE INDEX IF NOT EXISTS idx_audit_logs_business_created
            ON audit_logs (business_id, created_at DESC);

        -- "last active" in the admin panel reads the newest refresh per business.
        CREATE INDEX IF NOT EXISTS idx_refresh_tokens_business_created
            ON refresh_tokens (business_id, created_at DESC);
        """
    )

    # Default plans (placeholder prices; the owner edits them in the admin panel).
    op.execute(
        """
        INSERT INTO plans (name, monthly_price_pkr, onetime_price_pkr, notes)
        VALUES
            ('Basic', 3000, 0, 'Placeholder price. Every plan includes every feature.'),
            ('Multi-branch', 6000, 0, 'Placeholder price. Every plan includes every feature.')
        ON CONFLICT (name) DO NOTHING;

        INSERT INTO platform_settings (key, value) VALUES
            ('contact_phone', ''),
            ('contact_whatsapp', ''),
            ('contact_email', ''),
            ('payment_instructions', '')
        ON CONFLICT (key) DO NOTHING;
        """
    )

    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_refresh_tokens_business_created;
        DROP INDEX IF EXISTS idx_audit_logs_business_created;
        ALTER TABLE audit_logs DROP COLUMN IF EXISTS branch_id;
        DROP TABLE IF EXISTS licenses;
        DROP TABLE IF EXISTS platform_business_notes;
        DROP TABLE IF EXISTS platform_audit_log;
        DROP TABLE IF EXISTS platform_settings;
        DROP TABLE IF EXISTS subscription_payments;
        DROP INDEX IF EXISTS idx_businesses_subscription;
        DROP INDEX IF EXISTS idx_businesses_plan_id;
        ALTER TABLE businesses DROP COLUMN IF EXISTS plan_id;
        DROP TABLE IF EXISTS plans;
        DROP TABLE IF EXISTS platform_admins;
        """
    )
