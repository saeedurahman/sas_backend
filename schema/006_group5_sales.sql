-- =============================================================================
-- GROUP 5 — SALES
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- customers
-- WHY: Customer master for loyalty, credit sales, and CRM.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE customers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    email               VARCHAR(255),
    phone               VARCHAR(50),
    tax_id              VARCHAR(100),
    address_line1       VARCHAR(255),
    city                VARCHAR(100),
    credit_limit        NUMERIC(15,2) NOT NULL DEFAULT 0,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX idx_customers_business_id ON customers (business_id);
CREATE INDEX idx_customers_phone ON customers (business_id, phone) WHERE deleted_at IS NULL;
CREATE INDEX idx_customers_name ON customers (business_id, name);


-- ─────────────────────────────────────────────────────────────────────────────
-- customer_ledger
-- WHY: Append-only customer account entries. Balance = SUM(amount) per customer.
--      Positive amount = customer owes business; negative = credit/overpayment.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE customer_ledger (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id         UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    entry_type          ledger_entry_type_enum NOT NULL,
    amount              NUMERIC(15,2) NOT NULL,          -- signed: + debit (owes), - credit
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
    CONSTRAINT chk_customer_ledger_amount CHECK (amount <> 0)
);

CREATE INDEX idx_customer_ledger_business_id ON customer_ledger (business_id);
CREATE INDEX idx_customer_ledger_customer ON customer_ledger (business_id, customer_id, entry_at);
CREATE INDEX idx_customer_ledger_reference ON customer_ledger (business_id, reference_type, reference_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- tax_rates
-- WHY: Configurable tax rates (GST 5%, 12%, 18%, VAT, etc.).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE tax_rates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    rate                NUMERIC(5,2) NOT NULL,
    is_compound         BOOLEAN NOT NULL DEFAULT FALSE,
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
    CONSTRAINT uq_tax_rates_business_name UNIQUE (business_id, name),
    CONSTRAINT chk_tax_rates_rate CHECK (rate >= 0 AND rate <= 100)
);

CREATE INDEX idx_tax_rates_business_id ON tax_rates (business_id);

ALTER TABLE products
    ADD CONSTRAINT fk_products_tax_rate
    FOREIGN KEY (tax_rate_id) REFERENCES tax_rates(id) ON DELETE SET NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- discount_schemes
-- WHY: Promotional discount rules (percentage or fixed, date-bounded).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE discount_schemes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    discount_type       discount_type_enum NOT NULL,
    discount_value      NUMERIC(15,2) NOT NULL,
    min_purchase_amount NUMERIC(15,2),
    valid_from          TIMESTAMPTZ,
    valid_to            TIMESTAMPTZ,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    applies_to_json     JSONB NOT NULL DEFAULT '{}',     -- category_ids, product_ids filters
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT chk_discount_schemes_value CHECK (discount_value >= 0)
);

CREATE INDEX idx_discount_schemes_business_id ON discount_schemes (business_id);
CREATE INDEX idx_discount_schemes_active ON discount_schemes (business_id, is_active, valid_from, valid_to)
    WHERE deleted_at IS NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- sales
-- WHY: Sale transaction header. Totals derived from sale_lines + sale_payments.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sales (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    customer_id         UUID REFERENCES customers(id) ON DELETE SET NULL,
    register_shift_id   UUID,                            -- FK added in GROUP 8
    price_list_id       UUID REFERENCES price_lists(id) ON DELETE SET NULL,
    sale_number         VARCHAR(50) NOT NULL,
    sale_type           sale_type_enum NOT NULL DEFAULT 'pos',
    status              sale_status_enum NOT NULL DEFAULT 'draft',
    sold_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes               TEXT,
    discount_scheme_id  UUID REFERENCES discount_schemes(id) ON DELETE SET NULL,
    table_id            UUID,                            -- FK added in GROUP 6 (restaurant)
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_sales_number UNIQUE (business_id, sale_number)
);

CREATE INDEX idx_sales_business_id ON sales (business_id);
CREATE INDEX idx_sales_branch_date ON sales (business_id, branch_id, sold_at);
CREATE INDEX idx_sales_customer ON sales (business_id, customer_id);
CREATE INDEX idx_sales_status ON sales (business_id, status, sold_at);
CREATE INDEX idx_sales_sync ON sales (business_id, sync_status) WHERE sync_status <> 'synced';
CREATE INDEX idx_sales_local_id ON sales (business_id, local_id) WHERE local_id IS NOT NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- sale_lines
-- WHY: Line items. Each completed line triggers stock_movement (type=sale).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sale_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    sale_id             UUID NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty                 NUMERIC(15,4) NOT NULL,
    unit_price          NUMERIC(15,2) NOT NULL,
    discount_pct        NUMERIC(5,2) NOT NULL DEFAULT 0,
    discount_amount     NUMERIC(15,2) NOT NULL DEFAULT 0,
    tax_rate            NUMERIC(5,2) NOT NULL DEFAULT 0,
    tax_amount          NUMERIC(15,2) NOT NULL DEFAULT 0,
    cost_per_unit       NUMERIC(15,2) NOT NULL DEFAULT 0, -- [DENORMALIZED] FIFO COGS at sale time
    notes               TEXT,                            -- kitchen notes, modifiers summary
    line_order          INTEGER NOT NULL DEFAULT 0,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_sale_lines_qty CHECK (qty > 0),
    CONSTRAINT chk_sale_lines_price CHECK (unit_price >= 0),
    CONSTRAINT chk_sale_lines_discount_pct CHECK (discount_pct >= 0 AND discount_pct <= 100)
);

COMMENT ON COLUMN sale_lines.cost_per_unit IS '[DENORMALIZED - performance cache] Snapshot of FIFO cost at sale time.';

CREATE INDEX idx_sale_lines_business_id ON sale_lines (business_id);
CREATE INDEX idx_sale_lines_sale ON sale_lines (business_id, sale_id);
CREATE INDEX idx_sale_lines_product ON sale_lines (business_id, product_id, variation_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- sale_payments
-- WHY: One sale may have multiple payments (split: cash + UPI).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sale_payments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    sale_id             UUID NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    payment_method      payment_method_enum NOT NULL,
    amount              NUMERIC(15,2) NOT NULL,
    status              payment_status_enum NOT NULL DEFAULT 'completed',
    reference_no        VARCHAR(100),                    -- UPI ref, card auth code
    paid_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_sale_payments_amount CHECK (amount > 0)
);

CREATE INDEX idx_sale_payments_business_id ON sale_payments (business_id);
CREATE INDEX idx_sale_payments_sale ON sale_payments (business_id, sale_id);
CREATE INDEX idx_sale_payments_method ON sale_payments (business_id, payment_method, paid_at);


-- ─────────────────────────────────────────────────────────────────────────────
-- sale_returns
-- WHY: Customer return header linked to original sale (optional).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sale_returns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    sale_id             UUID REFERENCES sales(id) ON DELETE SET NULL,
    customer_id         UUID REFERENCES customers(id) ON DELETE SET NULL,
    return_number       VARCHAR(50) NOT NULL,
    returned_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason              TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_sale_returns_number UNIQUE (business_id, return_number)
);

CREATE INDEX idx_sale_returns_business_id ON sale_returns (business_id);
CREATE INDEX idx_sale_returns_sale ON sale_returns (business_id, sale_id);
CREATE INDEX idx_sale_returns_date ON sale_returns (business_id, branch_id, returned_at);


-- ─────────────────────────────────────────────────────────────────────────────
-- sale_return_lines
-- WHY: Returned items. Creates stock_movement (type=sale_return).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sale_return_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    sale_return_id      UUID NOT NULL REFERENCES sale_returns(id) ON DELETE CASCADE,
    sale_line_id        UUID REFERENCES sale_lines(id) ON DELETE SET NULL,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty                 NUMERIC(15,4) NOT NULL,
    unit_price          NUMERIC(15,2) NOT NULL,
    tax_amount          NUMERIC(15,2) NOT NULL DEFAULT 0,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_sale_return_lines_qty CHECK (qty > 0)
);

CREATE INDEX idx_sale_return_lines_business_id ON sale_return_lines (business_id);
CREATE INDEX idx_sale_return_lines_header ON sale_return_lines (business_id, sale_return_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- sale_return_payments
-- WHY: Refund payments issued for a return.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sale_return_payments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    sale_return_id      UUID NOT NULL REFERENCES sale_returns(id) ON DELETE CASCADE,
    payment_method      payment_method_enum NOT NULL,
    amount              NUMERIC(15,2) NOT NULL,
    status              payment_status_enum NOT NULL DEFAULT 'completed',
    refunded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_sale_return_payments_amount CHECK (amount > 0)
);

CREATE INDEX idx_sale_return_payments_business_id ON sale_return_payments (business_id);
CREATE INDEX idx_sale_return_payments_return ON sale_return_payments (business_id, sale_return_id);

-- Relationship notes (GROUP 5):
-- customers belongs to businesses; has many sales, customer_ledger entries
-- customer_ledger belongs to customer; append-only balance history
-- tax_rates belongs to businesses; referenced by products, sale_lines
-- discount_schemes belongs to businesses; referenced by sales
-- sales belongs to branch, customer; has many sale_lines, sale_payments
-- sale_lines belongs to sale; triggers stock_movements (sale)
-- sale_returns belongs to sale; has many sale_return_lines, sale_return_payments
-- sale_return_lines triggers stock_movements (sale_return)
