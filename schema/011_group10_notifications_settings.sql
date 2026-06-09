-- =============================================================================
-- GROUP 10 — NOTIFICATIONS & SETTINGS
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- notification_log
-- WHY: System-generated alerts (low stock, expiry, sync conflicts).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE notification_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES branches(id) ON DELETE SET NULL,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,  -- target user (null = all)
    notification_type   notification_type_enum NOT NULL,
    channel             notification_channel_enum NOT NULL DEFAULT 'in_app',
    title               VARCHAR(255) NOT NULL,
    body                TEXT,
    payload_json        JSONB NOT NULL DEFAULT '{}',     -- product_id, threshold, etc.
    is_read             BOOLEAN NOT NULL DEFAULT FALSE,
    read_at             TIMESTAMPTZ,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX idx_notification_log_business_id ON notification_log (business_id);
CREATE INDEX idx_notification_log_user_unread ON notification_log (business_id, user_id, is_read)
    WHERE deleted_at IS NULL AND is_read = FALSE;
CREATE INDEX idx_notification_log_type ON notification_log (business_id, notification_type, created_at);


-- ─────────────────────────────────────────────────────────────────────────────
-- app_settings
-- WHY: Per-business key-value configuration (receipt template, tax display, etc.).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE app_settings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES branches(id) ON DELETE CASCADE,  -- null = business-wide
    setting_key         VARCHAR(100) NOT NULL,
    setting_value       JSONB NOT NULL DEFAULT '{}',
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_app_settings_key UNIQUE (business_id, branch_id, setting_key)
);

CREATE INDEX idx_app_settings_business_id ON app_settings (business_id);
CREATE INDEX idx_app_settings_key ON app_settings (business_id, setting_key);


-- ─────────────────────────────────────────────────────────────────────────────
-- audit_logs
-- WHY: Immutable audit trail of who changed what (compliance & debugging).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE audit_logs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    action              audit_action_enum NOT NULL,
    table_name          VARCHAR(100) NOT NULL,
    record_id           UUID NOT NULL,
    old_values          JSONB,
    new_values          JSONB,
    ip_address          INET,
    user_agent          TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_logs_business_id ON audit_logs (business_id);
CREATE INDEX idx_audit_logs_record ON audit_logs (business_id, table_name, record_id);
CREATE INDEX idx_audit_logs_user ON audit_logs (business_id, user_id, created_at);
CREATE INDEX idx_audit_logs_created ON audit_logs (business_id, created_at);

-- Relationship notes (GROUP 10):
-- notification_log belongs to business, optional branch/user
-- app_settings belongs to business, optional branch
-- audit_logs belongs to business, optional user
