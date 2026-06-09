-- =============================================================================
-- GROUP 4 — MANUFACTURING / BOM
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- bom_headers
-- WHY: Bill of Materials / recipe header linked to a finished product.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE bom_headers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    output_qty          NUMERIC(15,4) NOT NULL DEFAULT 1, -- finished qty per BOM run
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    version             INTEGER NOT NULL DEFAULT 1,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT chk_bom_headers_output_qty CHECK (output_qty > 0)
);

CREATE INDEX idx_bom_headers_business_id ON bom_headers (business_id);
CREATE INDEX idx_bom_headers_product ON bom_headers (business_id, product_id, variation_id);
CREATE INDEX idx_bom_headers_active ON bom_headers (business_id, is_active) WHERE deleted_at IS NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- bom_lines
-- WHY: Raw material ingredients and quantities per BOM.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE bom_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    bom_header_id       UUID NOT NULL REFERENCES bom_headers(id) ON DELETE CASCADE,
    ingredient_product_id UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    ingredient_variation_id UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty_required        NUMERIC(15,4) NOT NULL,          -- qty consumed per output_qty of BOM
    unit_id             UUID REFERENCES units(id) ON DELETE SET NULL,
    wastage_pct         NUMERIC(5,2) NOT NULL DEFAULT 0, -- expected wastage %
    sort_order          INTEGER NOT NULL DEFAULT 0,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT chk_bom_lines_qty CHECK (qty_required > 0),
    CONSTRAINT chk_bom_lines_wastage CHECK (wastage_pct >= 0 AND wastage_pct <= 100)
);

CREATE INDEX idx_bom_lines_business_id ON bom_lines (business_id);
CREATE INDEX idx_bom_lines_header ON bom_lines (business_id, bom_header_id);
CREATE INDEX idx_bom_lines_ingredient ON bom_lines (business_id, ingredient_product_id, ingredient_variation_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- production_orders
-- WHY: Work order to produce X quantity of a finished good via BOM.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE production_orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    bom_header_id       UUID NOT NULL REFERENCES bom_headers(id) ON DELETE RESTRICT,
    production_number   VARCHAR(50) NOT NULL,
    status              production_order_status_enum NOT NULL DEFAULT 'draft',
    qty_to_produce      NUMERIC(15,4) NOT NULL,
    qty_produced        NUMERIC(15,4) NOT NULL DEFAULT 0,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
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
    CONSTRAINT uq_production_orders_number UNIQUE (business_id, production_number),
    CONSTRAINT chk_production_orders_qty CHECK (qty_to_produce > 0),
    CONSTRAINT chk_production_orders_produced CHECK (qty_produced >= 0 AND qty_produced <= qty_to_produce)
);

CREATE INDEX idx_production_orders_business_id ON production_orders (business_id);
CREATE INDEX idx_production_orders_branch ON production_orders (business_id, branch_id);
CREATE INDEX idx_production_orders_status ON production_orders (business_id, status);
CREATE INDEX idx_production_orders_bom ON production_orders (business_id, bom_header_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- production_lines
-- WHY: Actual raw materials consumed in a production run.
--      Each line creates stock_movement (production_out).
--      Completed order creates production_in for finished good.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE production_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    production_order_id UUID NOT NULL REFERENCES production_orders(id) ON DELETE CASCADE,
    bom_line_id         UUID REFERENCES bom_lines(id) ON DELETE SET NULL,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty_consumed        NUMERIC(15,4) NOT NULL,
    cost_per_unit       NUMERIC(15,2) NOT NULL DEFAULT 0,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_production_lines_qty CHECK (qty_consumed > 0)
);

CREATE INDEX idx_production_lines_business_id ON production_lines (business_id);
CREATE INDEX idx_production_lines_order ON production_lines (business_id, production_order_id);
CREATE INDEX idx_production_lines_product ON production_lines (business_id, product_id, variation_id);

-- Relationship notes (GROUP 4):
-- bom_headers belongs to product/variation; has many bom_lines
-- bom_lines belongs to bom_header + ingredient product
-- production_orders belongs to bom_header, branch; has many production_lines
-- production_lines belongs to production_order; triggers production_out movements
-- production_orders completion triggers production_in movement for finished good
