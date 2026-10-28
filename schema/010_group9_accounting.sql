-- =============================================================================
-- GROUP 9 — ACCOUNTING (Skeleton for double-entry)
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- chart_of_accounts
-- WHY: Chart of accounts per tenant. Foundation for journal entries.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE chart_of_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    parent_id           UUID REFERENCES chart_of_accounts(id) ON DELETE SET NULL,
    account_code        VARCHAR(20) NOT NULL,
    account_name        VARCHAR(255) NOT NULL,
    account_type        account_type_enum NOT NULL,
    account_subtype     account_subtype_enum,
    is_system           BOOLEAN NOT NULL DEFAULT FALSE,  -- seeded COA rows
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    description         TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_coa_code UNIQUE (business_id, account_code)
);

CREATE INDEX idx_chart_of_accounts_business_id ON chart_of_accounts (business_id);
CREATE INDEX idx_chart_of_accounts_type ON chart_of_accounts (business_id, account_type);
CREATE INDEX idx_chart_of_accounts_parent ON chart_of_accounts (business_id, parent_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- journal_entries
-- WHY: Double-entry transaction header. Every financial event posts here eventually.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE journal_entries (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES branches(id) ON DELETE SET NULL,
    entry_number        VARCHAR(50) NOT NULL,
    status              journal_entry_status_enum NOT NULL DEFAULT 'draft',
    entry_date          DATE NOT NULL DEFAULT CURRENT_DATE,
    description         TEXT,
    reference_type      reference_type_enum,             -- sale, purchase_receipt, expense, etc.
    reference_id        UUID,
    posted_at           TIMESTAMPTZ,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_journal_entries_number UNIQUE (business_id, entry_number)
);

CREATE INDEX idx_journal_entries_business_id ON journal_entries (business_id);
CREATE INDEX idx_journal_entries_date ON journal_entries (business_id, entry_date);
CREATE INDEX idx_journal_entries_status ON journal_entries (business_id, status);
CREATE INDEX idx_journal_entries_reference ON journal_entries (business_id, reference_type, reference_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- journal_lines
-- WHY: Debit/credit lines. SUM(debits) must equal SUM(credits) per journal_entry.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE journal_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    journal_entry_id    UUID NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id          UUID NOT NULL REFERENCES chart_of_accounts(id) ON DELETE RESTRICT,
    debit_amount        NUMERIC(15,2) NOT NULL DEFAULT 0,
    credit_amount       NUMERIC(15,2) NOT NULL DEFAULT 0,
    description         TEXT,
    line_order          INTEGER NOT NULL DEFAULT 0,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_journal_lines_one_side CHECK (
        (debit_amount > 0 AND credit_amount = 0) OR
        (credit_amount > 0 AND debit_amount = 0)
    ),
    CONSTRAINT chk_journal_lines_non_negative CHECK (debit_amount >= 0 AND credit_amount >= 0)
);

CREATE INDEX idx_journal_lines_business_id ON journal_lines (business_id);
CREATE INDEX idx_journal_lines_entry ON journal_lines (business_id, journal_entry_id);
CREATE INDEX idx_journal_lines_account ON journal_lines (business_id, account_id);

-- Relationship notes (GROUP 9):
-- chart_of_accounts belongs to businesses; self-ref parent; has many journal_lines
-- journal_entries belongs to business/branch; has many journal_lines
-- journal_lines belongs to journal_entry + chart_of_accounts account
