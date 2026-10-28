-- =============================================================================
-- GROUP 8 — CASH REGISTER & SHIFTS
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- cash_registers
-- WHY: Physical POS register terminal per branch.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE cash_registers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    device_identifier   VARCHAR(255),                    -- offline device UUID
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
    CONSTRAINT uq_cash_registers_name UNIQUE (business_id, branch_id, name)
);

CREATE INDEX idx_cash_registers_business_id ON cash_registers (business_id);
CREATE INDEX idx_cash_registers_branch ON cash_registers (business_id, branch_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- register_shifts
-- WHY: Open/close shift with opening float and closing cash count.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE register_shifts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    cash_register_id    UUID NOT NULL REFERENCES cash_registers(id) ON DELETE RESTRICT,
    opened_by           UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    closed_by           UUID REFERENCES users(id) ON DELETE SET NULL,
    status              shift_status_enum NOT NULL DEFAULT 'open',
    opening_float       NUMERIC(15,2) NOT NULL DEFAULT 0,
    expected_cash       NUMERIC(15,2),                   -- [DENORMALIZED] computed at close
    actual_cash         NUMERIC(15,2),                   -- counted cash at close
    cash_difference     NUMERIC(15,2),                   -- [DENORMALIZED] actual - expected
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ,
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL
);

COMMENT ON COLUMN register_shifts.expected_cash IS '[DENORMALIZED - performance cache] Sum of cash txns during shift.';
COMMENT ON COLUMN register_shifts.cash_difference IS '[DENORMALIZED - performance cache] actual_cash - expected_cash.';

CREATE INDEX idx_register_shifts_business_id ON register_shifts (business_id);
CREATE INDEX idx_register_shifts_register ON register_shifts (business_id, cash_register_id, status);
CREATE INDEX idx_register_shifts_open ON register_shifts (business_id, branch_id, opened_at)
    WHERE status = 'open';

ALTER TABLE sales
    ADD CONSTRAINT fk_sales_register_shift
    FOREIGN KEY (register_shift_id) REFERENCES register_shifts(id) ON DELETE SET NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- register_transactions
-- WHY: Every cash in/out event during a shift (audit trail for drawer).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE register_transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    register_shift_id   UUID NOT NULL REFERENCES register_shifts(id) ON DELETE CASCADE,
    tx_type             register_tx_type_enum NOT NULL,
    payment_method      payment_method_enum NOT NULL DEFAULT 'cash',
    amount              NUMERIC(15,2) NOT NULL,          -- positive = in, negative = out
    reference_type      reference_type_enum,
    reference_id        UUID,
    notes               TEXT,
    transacted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_register_tx_amount CHECK (amount <> 0)
);

CREATE INDEX idx_register_transactions_business_id ON register_transactions (business_id);
CREATE INDEX idx_register_transactions_shift ON register_transactions (business_id, register_shift_id, transacted_at);
CREATE INDEX idx_register_transactions_type ON register_transactions (business_id, tx_type);
CREATE INDEX idx_register_transactions_reference ON register_transactions (business_id, reference_type, reference_id);

-- Relationship notes (GROUP 8):
-- cash_registers belongs to branch; has many register_shifts
-- register_shifts belongs to cash_register; has many register_transactions, sales
-- register_transactions belongs to register_shift; references sale/expense/etc.
