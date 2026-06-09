-- =============================================================================
-- GROUP 7 — EXPENSES & PURCHASES (supplier_ledger)
-- Note: suppliers defined in GROUP 3
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- expense_categories
-- WHY: Classify expenses (rent, utilities, salaries) for reporting.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE expense_categories (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    parent_id           UUID REFERENCES expense_categories(id) ON DELETE SET NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_expense_categories_name UNIQUE (business_id, name)
);

CREATE INDEX idx_expense_categories_business_id ON expense_categories (business_id);
CREATE INDEX idx_expense_categories_parent ON expense_categories (business_id, parent_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- expenses
-- WHY: Business expense transactions (non-COGS operating costs).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE expenses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    expense_category_id UUID NOT NULL REFERENCES expense_categories(id) ON DELETE RESTRICT,
    supplier_id         UUID REFERENCES suppliers(id) ON DELETE SET NULL,
    expense_number      VARCHAR(50) NOT NULL,
    description         TEXT,
    expense_date        DATE NOT NULL DEFAULT CURRENT_DATE,
    amount              NUMERIC(15,2) NOT NULL,
    tax_amount          NUMERIC(15,2) NOT NULL DEFAULT 0,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_expenses_number UNIQUE (business_id, expense_number),
    CONSTRAINT chk_expenses_amount CHECK (amount > 0)
);

CREATE INDEX idx_expenses_business_id ON expenses (business_id);
CREATE INDEX idx_expenses_branch_date ON expenses (business_id, branch_id, expense_date);
CREATE INDEX idx_expenses_category ON expenses (business_id, expense_category_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- expense_payments
-- WHY: Payments made against expenses (may differ from expense amount if partial).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE expense_payments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    expense_id          UUID NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    payment_method      payment_method_enum NOT NULL,
    amount              NUMERIC(15,2) NOT NULL,
    status              payment_status_enum NOT NULL DEFAULT 'completed',
    paid_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reference_no        VARCHAR(100),
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_expense_payments_amount CHECK (amount > 0)
);

CREATE INDEX idx_expense_payments_business_id ON expense_payments (business_id);
CREATE INDEX idx_expense_payments_expense ON expense_payments (business_id, expense_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- supplier_ledger
-- WHY: Append-only supplier account entries. Balance = SUM(amount) per supplier.
--      Positive = business owes supplier; negative = credit/advance paid.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE supplier_ledger (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    supplier_id         UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    entry_type          ledger_entry_type_enum NOT NULL,
    amount              NUMERIC(15,2) NOT NULL,
    reference_type      reference_type_enum,
    reference_id        UUID,
    entry_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_supplier_ledger_amount CHECK (amount <> 0)
);

CREATE INDEX idx_supplier_ledger_business_id ON supplier_ledger (business_id);
CREATE INDEX idx_supplier_ledger_supplier ON supplier_ledger (business_id, supplier_id, entry_at);
CREATE INDEX idx_supplier_ledger_reference ON supplier_ledger (business_id, reference_type, reference_id);

-- Relationship notes (GROUP 7):
-- expense_categories belongs to businesses; self-ref parent_id
-- expenses belongs to branch, category, optional supplier; has many expense_payments
-- expense_payments belongs to expense
-- suppliers has many purchase_orders, supplier_ledger entries
-- supplier_ledger belongs to supplier; append-only AP history
