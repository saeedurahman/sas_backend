-- =============================================================================
-- GROUP 1 — TENANT & AUTH
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- business_types
-- WHY: Global lookup for business classification (onboarding presets only).
--      NOT used at runtime to gate features — see business_configs.config_json.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE business_types (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code                VARCHAR(50) UNIQUE NOT NULL,
    name                VARCHAR(100) NOT NULL,
    description         TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order          SMALLINT DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_business_types_code ON business_types (code) WHERE is_active = TRUE;

-- Seed lookup rows before businesses (see also seeds/002_business_types.sql)
INSERT INTO business_types (code, name, description, sort_order)
VALUES
    ('bakery',      'Bakery / Sweets',     'Roti, cakes, sweets, confectionery', 1),
    ('restaurant',  'Restaurant / Cafe',   'Dine-in, takeaway, fast food',        2),
    ('mart',        'Mart / Grocery',      'General grocery and daily items',     3),
    ('retail',      'Retail Store',        'Clothing, shoes, general retail',     4),
    ('hardware',    'Hardware Store',      'Building materials, tools',           5),
    ('pharmacy',    'Pharmacy',            'Medical store, medicines',            6),
    ('wholesale',   'Wholesale',           'Bulk distribution business',          7),
    ('electronics', 'Electronics',         'Phones, gadgets, accessories',        8),
    ('salon',       'Salon / Parlour',     'Beauty salon, barber shop',           9),
    ('other',       'Other',               'Any other business type',            99)
ON CONFLICT (code) DO NOTHING;


-- ─────────────────────────────────────────────────────────────────────────────
-- businesses
-- WHY: Root tenant entity. Every other row is scoped to a business_id.
--      business_type_id is classification only; features live in business_configs.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE businesses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(255) NOT NULL,
    legal_name          VARCHAR(255),
    business_type_id    UUID NOT NULL REFERENCES business_types(id) ON DELETE RESTRICT,
    tax_id              VARCHAR(100),                    -- GSTIN / VAT number
    email               VARCHAR(255),
    phone               VARCHAR(50),
    address_line1       VARCHAR(255),
    address_line2       VARCHAR(255),
    city                VARCHAR(100),
    state               VARCHAR(100),
    postal_code         VARCHAR(20),
    country_code        CHAR(2) NOT NULL DEFAULT 'PK',
    currency_code       CHAR(3) NOT NULL DEFAULT 'PKR',
    timezone            VARCHAR(64) NOT NULL DEFAULT 'Asia/Karachi',
    logo_url            TEXT,
    subscription_plan   subscription_plan_enum NOT NULL DEFAULT 'trial',
    subscription_status subscription_status_enum NOT NULL DEFAULT 'trial',
    trial_ends_at       TIMESTAMPTZ,
    subscription_ends_at TIMESTAMPTZ,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    -- offline sync
    local_id            UUID,                            -- client idempotency key
    server_id           UUID UNIQUE,                     -- canonical server id post-sync
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    -- audit / soft delete
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID,                            -- no FK: avoids circular dep with users
    updated_by          UUID,
    deleted_by          UUID
);

COMMENT ON TABLE businesses IS 'Tenant root. No business_id column — this IS the tenant.';
COMMENT ON COLUMN businesses.server_id IS 'Populated on sync; often equals id for server-origin records.';

CREATE INDEX idx_businesses_sync_status ON businesses (sync_status) WHERE deleted_at IS NULL;
CREATE INDEX idx_businesses_subscription ON businesses (subscription_status, subscription_plan)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_businesses_local_id ON businesses (local_id) WHERE local_id IS NOT NULL;
CREATE INDEX idx_businesses_business_type_id ON businesses (business_type_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- business_configs
-- WHY: Per-tenant feature flags. Runtime feature checks use config_json only.
--      Boolean columns are optional mirrors; onboarding may set both once.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE business_configs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    -- feature flags (extend as modules grow)
    enable_restaurant   BOOLEAN NOT NULL DEFAULT FALSE,
    enable_manufacturing BOOLEAN NOT NULL DEFAULT FALSE,
    enable_loyalty      BOOLEAN NOT NULL DEFAULT FALSE,
    enable_multi_price_list BOOLEAN NOT NULL DEFAULT FALSE,
    enable_batch_tracking BOOLEAN NOT NULL DEFAULT FALSE,
    enable_expiry_tracking BOOLEAN NOT NULL DEFAULT FALSE,
    enable_weight_billing BOOLEAN NOT NULL DEFAULT FALSE,
    enable_table_management BOOLEAN NOT NULL DEFAULT FALSE,
    enable_kot            BOOLEAN NOT NULL DEFAULT FALSE,
    enable_offline_mode   BOOLEAN NOT NULL DEFAULT TRUE,
    enable_accounting     BOOLEAN NOT NULL DEFAULT FALSE,
    default_tax_inclusive BOOLEAN NOT NULL DEFAULT FALSE,
    allow_negative_stock  BOOLEAN NOT NULL DEFAULT FALSE,
    fifo_costing_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    receipt_prefix        VARCHAR(20) DEFAULT 'RCP',
    invoice_prefix        VARCHAR(20) DEFAULT 'INV',
    po_prefix             VARCHAR(20) DEFAULT 'PO',
    config_json           JSONB NOT NULL DEFAULT '{}',   -- extensible key-value overrides
    local_id              UUID,
    server_id             UUID,
    sync_status           sync_status_enum NOT NULL DEFAULT 'synced',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at            TIMESTAMPTZ,
    created_by            UUID,
    updated_by            UUID,
    deleted_by            UUID,
    CONSTRAINT uq_business_configs_business UNIQUE (business_id)
);

CREATE INDEX idx_business_configs_business_id ON business_configs (business_id);
CREATE INDEX idx_business_configs_sync ON business_configs (business_id, sync_status);


-- ─────────────────────────────────────────────────────────────────────────────
-- branches
-- WHY: Physical locations (1–5 per business). Stock, registers, and sales
--      are always branch-scoped.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE branches (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    code                VARCHAR(20),                     -- short code e.g. BR01
    is_head_office      BOOLEAN NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    address_line1       VARCHAR(255),
    address_line2       VARCHAR(255),
    city                VARCHAR(100),
    state               VARCHAR(100),
    postal_code         VARCHAR(20),
    phone               VARCHAR(50),
    email               VARCHAR(255),
    local_id            UUID,
    server_id             UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID,
    updated_by          UUID,
    deleted_by          UUID,
    CONSTRAINT uq_branches_business_code UNIQUE (business_id, code)
);

CREATE INDEX idx_branches_business_id ON branches (business_id);
CREATE INDEX idx_branches_business_active ON branches (business_id, is_active)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_branches_local_id ON branches (business_id, local_id)
    WHERE local_id IS NOT NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- users
-- WHY: Operators (owner, manager, cashier). Authenticated per business.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    default_branch_id   UUID REFERENCES branches(id) ON DELETE SET NULL,
    email               VARCHAR(255),
    phone               VARCHAR(50),
    password_hash       TEXT,                            -- bcrypt; null for SSO-only users
    full_name           VARCHAR(255) NOT NULL,
    pin_hash            TEXT,                            -- bcrypt hash for quick POS PIN (never plain text)
    is_locked           BOOLEAN NOT NULL DEFAULT FALSE,
    failed_login_attempts SMALLINT NOT NULL DEFAULT 0,
    locked_until        TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ,
    status              user_status_enum NOT NULL DEFAULT 'active',
    last_login_at       TIMESTAMPTZ,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_users_business_email UNIQUE (business_id, email),
    CONSTRAINT uq_users_business_phone UNIQUE (business_id, phone)
);

CREATE INDEX idx_users_business_id ON users (business_id);
CREATE INDEX idx_users_business_status ON users (business_id, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_users_default_branch ON users (business_id, default_branch_id);
CREATE INDEX idx_users_local_id ON users (business_id, local_id) WHERE local_id IS NOT NULL;
CREATE INDEX idx_users_locked ON users (business_id, is_locked) WHERE is_locked = TRUE;


-- ─────────────────────────────────────────────────────────────────────────────
-- refresh_tokens
-- WHY: JWT refresh token rotation / revocation (jti), scoped per user and tenant.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE refresh_tokens (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    jti                 VARCHAR(255) UNIQUE NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,
    ip_address          VARCHAR(45),
    user_agent          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE INDEX idx_refresh_tokens_jti ON refresh_tokens (jti);
CREATE INDEX idx_refresh_tokens_active ON refresh_tokens (user_id, expires_at)
    WHERE revoked_at IS NULL;
CREATE INDEX idx_refresh_tokens_business_id ON refresh_tokens (business_id);

-- deferred FK: businesses.updated_by / deleted_by only (created_by has no FK)
ALTER TABLE businesses
    ADD CONSTRAINT fk_businesses_updated_by FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_businesses_deleted_by FOREIGN KEY (deleted_by) REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE business_configs
    ADD CONSTRAINT fk_business_configs_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_business_configs_updated_by FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_business_configs_deleted_by FOREIGN KEY (deleted_by) REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE branches
    ADD CONSTRAINT fk_branches_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_branches_updated_by FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_branches_deleted_by FOREIGN KEY (deleted_by) REFERENCES users(id) ON DELETE SET NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- permissions
-- WHY: Global catalog of granular permission keys (system seed data).
--      Not tenant-scoped — shared across all businesses.
-- EXCEPTION: REQ-1 business_id omitted — system-wide reference table.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE permissions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    permission_key      VARCHAR(100) NOT NULL UNIQUE,    -- e.g. sales.create, inventory.adjust
    module              VARCHAR(50) NOT NULL,            -- sales, inventory, reports
    description         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_permissions_module ON permissions (module);


-- ─────────────────────────────────────────────────────────────────────────────
-- roles
-- WHY: Custom role definitions per tenant (Owner, Manager, Cashier, etc.).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE roles (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    description         TEXT,
    is_system           BOOLEAN NOT NULL DEFAULT FALSE,  -- seeded roles cannot be deleted
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_roles_business_name UNIQUE (business_id, name)
);

CREATE INDEX idx_roles_business_id ON roles (business_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- role_permissions
-- WHY: Many-to-many mapping of roles to permissions.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE role_permissions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    role_id             UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id       UUID NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_role_permissions UNIQUE (business_id, role_id, permission_id)
);

CREATE INDEX idx_role_permissions_business_id ON role_permissions (business_id);
CREATE INDEX idx_role_permissions_role ON role_permissions (business_id, role_id);
CREATE INDEX idx_role_permissions_permission ON role_permissions (permission_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- user_roles
-- WHY: Assigns one or more roles to a user within a tenant.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE user_roles (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id             UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES branches(id) ON DELETE CASCADE,  -- null = all branches
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_user_roles UNIQUE (business_id, user_id, role_id, branch_id)
);

CREATE INDEX idx_user_roles_business_id ON user_roles (business_id);
CREATE INDEX idx_user_roles_user ON user_roles (business_id, user_id);
CREATE INDEX idx_user_roles_role ON user_roles (business_id, role_id);

-- Relationship notes (GROUP 1):
-- business_types has many businesses
-- businesses belongs to business_types; has many branches, users, roles, business_configs (1:1)
-- business_configs belongs to businesses
-- branches belongs to businesses; has many sales, stock_movements, registers
-- users belongs to businesses; has many user_roles
-- roles belongs to businesses; has many role_permissions, user_roles
-- permissions is global; referenced by role_permissions
-- role_permissions belongs to roles + permissions
-- user_roles belongs to users + roles
