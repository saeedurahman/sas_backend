-- =============================================================================
-- GROUP 3 — INVENTORY (Movement-Ledger + FIFO)
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- stock_movements
-- WHY: THE inventory ledger — single source of truth. Every stock change creates
--      a row here. current_qty = SUM(qty) GROUP BY business, branch, product, variation.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE stock_movements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    movement_type       stock_movement_type_enum NOT NULL,
    qty                 NUMERIC(15,4) NOT NULL,          -- positive = in, negative = out
    cost_per_unit       NUMERIC(15,2) NOT NULL DEFAULT 0,-- valuation at time of movement
    reference_type      reference_type_enum NOT NULL,
    reference_id        UUID NOT NULL,                   -- polymorphic FK to source line/header
    purchase_line_id    UUID,                            -- FK added below; links FIFO layer consumption
    batch_number        VARCHAR(100),                    -- optional batch tracking
    expiry_date         DATE,                            -- optional expiry tracking
    notes               TEXT,
    movement_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(), -- business event time (may differ from created_at)
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_stock_movements_qty_nonzero CHECK (qty <> 0)
);

COMMENT ON TABLE stock_movements IS 'Append-only ledger. Never UPDATE qty — reverse with opposing movement.';
COMMENT ON COLUMN stock_movements.qty IS 'Positive = stock in, negative = stock out.';
COMMENT ON COLUMN stock_movements.purchase_line_id IS 'When movement consumes a FIFO layer, points to purchase_lines.id.';

CREATE INDEX idx_stock_movements_business_id ON stock_movements (business_id);
CREATE INDEX idx_stock_movements_ledger ON stock_movements (
    business_id, branch_id, product_id, variation_id, movement_at
) WHERE deleted_at IS NULL;
CREATE INDEX idx_stock_movements_type ON stock_movements (business_id, movement_type, movement_at);
CREATE INDEX idx_stock_movements_reference ON stock_movements (business_id, reference_type, reference_id);
CREATE INDEX idx_stock_movements_sync ON stock_movements (business_id, sync_status)
    WHERE sync_status <> 'synced';
CREATE INDEX idx_stock_movements_local_id ON stock_movements (business_id, local_id)
    WHERE local_id IS NOT NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- suppliers
-- WHY: Vendor master — placed here before POs (also in GROUP 7, defined once).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE suppliers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    contact_person      VARCHAR(255),
    email               VARCHAR(255),
    phone               VARCHAR(50),
    tax_id              VARCHAR(100),
    address_line1       VARCHAR(255),
    city                VARCHAR(100),
    payment_terms_days  INTEGER NOT NULL DEFAULT 0,
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

CREATE INDEX idx_suppliers_business_id ON suppliers (business_id);
CREATE INDEX idx_suppliers_active ON suppliers (business_id, is_active) WHERE deleted_at IS NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- purchase_orders
-- WHY: PO header — intent to purchase before goods arrive.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE purchase_orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    supplier_id         UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    po_number           VARCHAR(50) NOT NULL,
    status              purchase_order_status_enum NOT NULL DEFAULT 'draft',
    ordered_at          TIMESTAMPTZ,
    expected_at         TIMESTAMPTZ,
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_purchase_orders_number UNIQUE (business_id, po_number)
);

CREATE INDEX idx_purchase_orders_business_id ON purchase_orders (business_id);
CREATE INDEX idx_purchase_orders_supplier ON purchase_orders (business_id, supplier_id);
CREATE INDEX idx_purchase_orders_status ON purchase_orders (business_id, status, ordered_at);
CREATE INDEX idx_purchase_orders_branch ON purchase_orders (business_id, branch_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- purchase_lines
-- WHY: PO line items AND FIFO cost layers. qty_remaining tracks unconsumed stock.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE purchase_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    purchase_order_id   UUID NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    ordered_qty         NUMERIC(15,4) NOT NULL,
    received_qty        NUMERIC(15,4) NOT NULL DEFAULT 0,
    qty_remaining       NUMERIC(15,4) NOT NULL DEFAULT 0, -- FIFO layer: unconsumed qty
    cost_per_unit       NUMERIC(15,2) NOT NULL,           -- landed cost per base unit
    tax_rate            NUMERIC(5,2) NOT NULL DEFAULT 0,
    batch_number        VARCHAR(100),
    expiry_date         DATE,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT chk_purchase_lines_ordered_qty CHECK (ordered_qty > 0),
    CONSTRAINT chk_purchase_lines_received_qty CHECK (received_qty >= 0),
    CONSTRAINT chk_purchase_lines_remaining CHECK (qty_remaining >= 0 AND qty_remaining <= received_qty),
    CONSTRAINT chk_purchase_lines_cost CHECK (cost_per_unit >= 0)
);

COMMENT ON TABLE purchase_lines IS 'FIFO cost layers. qty_remaining decremented on sale via stock_movements.purchase_line_id.';
COMMENT ON COLUMN purchase_lines.qty_remaining IS 'Unconsumed quantity in this cost layer (FIFO).';

CREATE INDEX idx_purchase_lines_business_id ON purchase_lines (business_id);
CREATE INDEX idx_purchase_lines_po ON purchase_lines (business_id, purchase_order_id);
CREATE INDEX idx_purchase_lines_fifo ON purchase_lines (
    business_id, product_id, variation_id, created_at
) WHERE qty_remaining > 0 AND deleted_at IS NULL;
CREATE INDEX idx_purchase_lines_product ON purchase_lines (business_id, product_id, variation_id);

ALTER TABLE stock_movements
    ADD CONSTRAINT fk_stock_movements_purchase_line
    FOREIGN KEY (purchase_line_id) REFERENCES purchase_lines(id) ON DELETE SET NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- purchase_receipts
-- WHY: GRN header — records actual goods received against a PO.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE purchase_receipts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    purchase_order_id   UUID REFERENCES purchase_orders(id) ON DELETE SET NULL,
    supplier_id         UUID NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    receipt_number      VARCHAR(50) NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    supplier_invoice_no VARCHAR(100),
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_purchase_receipts_number UNIQUE (business_id, receipt_number)
);

CREATE INDEX idx_purchase_receipts_business_id ON purchase_receipts (business_id);
CREATE INDEX idx_purchase_receipts_po ON purchase_receipts (business_id, purchase_order_id);
CREATE INDEX idx_purchase_receipts_supplier ON purchase_receipts (business_id, supplier_id);
CREATE INDEX idx_purchase_receipts_received ON purchase_receipts (business_id, received_at);


-- ─────────────────────────────────────────────────────────────────────────────
-- purchase_receipt_lines
-- WHY: Line-level receipt detail. Each line triggers stock_movement (type=purchase).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE purchase_receipt_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    purchase_receipt_id UUID NOT NULL REFERENCES purchase_receipts(id) ON DELETE CASCADE,
    purchase_line_id    UUID REFERENCES purchase_lines(id) ON DELETE SET NULL,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty_received        NUMERIC(15,4) NOT NULL,
    cost_per_unit       NUMERIC(15,2) NOT NULL,
    batch_number        VARCHAR(100),
    expiry_date         DATE,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_receipt_lines_qty CHECK (qty_received > 0),
    CONSTRAINT chk_receipt_lines_cost CHECK (cost_per_unit >= 0)
);

CREATE INDEX idx_purchase_receipt_lines_business_id ON purchase_receipt_lines (business_id);
CREATE INDEX idx_purchase_receipt_lines_receipt ON purchase_receipt_lines (business_id, purchase_receipt_id);
CREATE INDEX idx_purchase_receipt_lines_product ON purchase_receipt_lines (business_id, product_id, variation_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- stock_adjustments
-- WHY: Manual stock corrections with documented reason.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE stock_adjustments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    adjustment_number   VARCHAR(50) NOT NULL,
    reason              adjustment_reason_enum NOT NULL DEFAULT 'count_correction',
    adjusted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_stock_adjustments_number UNIQUE (business_id, adjustment_number)
);

CREATE INDEX idx_stock_adjustments_business_id ON stock_adjustments (business_id);
CREATE INDEX idx_stock_adjustments_branch ON stock_adjustments (business_id, branch_id, adjusted_at);


-- ─────────────────────────────────────────────────────────────────────────────
-- stock_adjustment_lines
-- WHY: Per-product adjustment. Creates adjustment_in or adjustment_out movement.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE stock_adjustment_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    stock_adjustment_id UUID NOT NULL REFERENCES stock_adjustments(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty_delta           NUMERIC(15,4) NOT NULL,          -- signed: + increase, - decrease
    cost_per_unit       NUMERIC(15,2) NOT NULL DEFAULT 0,
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_adjustment_lines_qty CHECK (qty_delta <> 0)
);

CREATE INDEX idx_stock_adjustment_lines_business_id ON stock_adjustment_lines (business_id);
CREATE INDEX idx_stock_adjustment_lines_header ON stock_adjustment_lines (business_id, stock_adjustment_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- stock_transfers
-- WHY: Inter-branch stock movement header (warehouse transfers).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE stock_transfers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    transfer_number     VARCHAR(50) NOT NULL,
    source_branch_id    UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    dest_branch_id      UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    status              transfer_status_enum NOT NULL DEFAULT 'draft',
    transferred_at      TIMESTAMPTZ,
    received_at         TIMESTAMPTZ,
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_stock_transfers_number UNIQUE (business_id, transfer_number),
    CONSTRAINT chk_stock_transfers_branches CHECK (source_branch_id <> dest_branch_id)
);

CREATE INDEX idx_stock_transfers_business_id ON stock_transfers (business_id);
CREATE INDEX idx_stock_transfers_source ON stock_transfers (business_id, source_branch_id);
CREATE INDEX idx_stock_transfers_dest ON stock_transfers (business_id, dest_branch_id);
CREATE INDEX idx_stock_transfers_status ON stock_transfers (business_id, status);


-- ─────────────────────────────────────────────────────────────────────────────
-- stock_transfer_lines
-- WHY: Items in a transfer. Each line creates transfer_out + transfer_in movements.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE stock_transfer_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    stock_transfer_id   UUID NOT NULL REFERENCES stock_transfers(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty                 NUMERIC(15,4) NOT NULL,
    cost_per_unit       NUMERIC(15,2) NOT NULL DEFAULT 0,
    qty_received        NUMERIC(15,4) NOT NULL DEFAULT 0, -- may differ on partial receive
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_transfer_lines_qty CHECK (qty > 0),
    CONSTRAINT chk_transfer_lines_received CHECK (qty_received >= 0 AND qty_received <= qty)
);

CREATE INDEX idx_stock_transfer_lines_business_id ON stock_transfer_lines (business_id);
CREATE INDEX idx_stock_transfer_lines_header ON stock_transfer_lines (business_id, stock_transfer_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- waste_entries
-- WHY: Spoilage/damage write-off header (bakery expiry, restaurant waste).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE waste_entries (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    waste_number        VARCHAR(50) NOT NULL,
    wasted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason              adjustment_reason_enum NOT NULL DEFAULT 'expiry',
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_waste_entries_number UNIQUE (business_id, waste_number)
);

CREATE INDEX idx_waste_entries_business_id ON waste_entries (business_id);
CREATE INDEX idx_waste_entries_branch ON waste_entries (business_id, branch_id, wasted_at);


-- ─────────────────────────────────────────────────────────────────────────────
-- waste_entry_lines
-- WHY: Per-item waste. Creates stock_movement (type=waste, qty negative).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE waste_entry_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    waste_entry_id      UUID NOT NULL REFERENCES waste_entries(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty                 NUMERIC(15,4) NOT NULL,
    cost_per_unit       NUMERIC(15,2) NOT NULL DEFAULT 0,
    batch_number        VARCHAR(100),
    expiry_date         DATE,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_waste_lines_qty CHECK (qty > 0)
);

CREATE INDEX idx_waste_entry_lines_business_id ON waste_entry_lines (business_id);
CREATE INDEX idx_waste_entry_lines_header ON waste_entry_lines (business_id, waste_entry_id);

-- Relationship notes (GROUP 3):
-- stock_movements belongs to business, branch, product, variation; references polymorphic source
-- purchase_orders belongs to supplier, branch; has many purchase_lines, purchase_receipts
-- purchase_lines belongs to purchase_order; FIFO layer; referenced by stock_movements
-- purchase_receipts belongs to PO/supplier; has many purchase_receipt_lines
-- purchase_receipt_lines triggers stock_movements (purchase)
-- stock_adjustments has many stock_adjustment_lines → stock_movements
-- stock_transfers has many stock_transfer_lines → transfer_out + transfer_in movements
-- waste_entries has many waste_entry_lines → stock_movements (waste)
