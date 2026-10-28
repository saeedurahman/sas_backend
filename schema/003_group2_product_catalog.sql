-- =============================================================================
-- GROUP 2 — PRODUCT CATALOG
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- categories
-- WHY: Hierarchical product grouping (parent_id self-reference).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE categories (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    parent_id           UUID REFERENCES categories(id) ON DELETE SET NULL,
    name                VARCHAR(255) NOT NULL,
    slug                VARCHAR(255),
    sort_order          INTEGER NOT NULL DEFAULT 0,
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
    CONSTRAINT uq_categories_business_slug UNIQUE (business_id, slug)
);

CREATE INDEX idx_categories_business_id ON categories (business_id);
CREATE INDEX idx_categories_parent ON categories (business_id, parent_id);
CREATE INDEX idx_categories_active ON categories (business_id, is_active) WHERE deleted_at IS NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- brands
-- WHY: Optional brand attribution for products (grocery/retail).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE brands (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
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
    CONSTRAINT uq_brands_business_name UNIQUE (business_id, name)
);

CREATE INDEX idx_brands_business_id ON brands (business_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- units
-- WHY: Base measurement units (kg, pcs, litre, dozen).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE units (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(50) NOT NULL,            -- Kilogram
    symbol              VARCHAR(10) NOT NULL,            -- kg
    is_base_unit        BOOLEAN NOT NULL DEFAULT TRUE,   -- false for derived units
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_units_business_symbol UNIQUE (business_id, symbol)
);

CREATE INDEX idx_units_business_id ON units (business_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- unit_conversions
-- WHY: Converts between units (1 dozen = 12 pcs) for purchasing vs selling.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE unit_conversions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    from_unit_id        UUID NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    to_unit_id          UUID NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    conversion_factor   NUMERIC(15,6) NOT NULL,          -- multiply from_qty * factor = to_qty
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_unit_conversions UNIQUE (business_id, from_unit_id, to_unit_id),
    CONSTRAINT chk_unit_conversions_factor CHECK (conversion_factor > 0),
    CONSTRAINT chk_unit_conversions_different CHECK (from_unit_id <> to_unit_id)
);

CREATE INDEX idx_unit_conversions_business_id ON unit_conversions (business_id);
CREATE INDEX idx_unit_conversions_from ON unit_conversions (business_id, from_unit_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- products
-- WHY: Master product record. Stock is tracked at variation level when variants exist.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE products (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    category_id         UUID REFERENCES categories(id) ON DELETE SET NULL,
    brand_id            UUID REFERENCES brands(id) ON DELETE SET NULL,
    base_unit_id        UUID NOT NULL REFERENCES units(id) ON DELETE RESTRICT,
    name                VARCHAR(255) NOT NULL,
    sku                 VARCHAR(100),
    product_type        product_type_enum NOT NULL DEFAULT 'standard',
    tracking_type       tracking_type_enum NOT NULL DEFAULT 'none',
    is_sellable         BOOLEAN NOT NULL DEFAULT TRUE,
    is_purchasable      BOOLEAN NOT NULL DEFAULT TRUE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    tax_rate_id         UUID,                            -- FK added after tax_rates created
    description         TEXT,
    image_url           TEXT,
    shelf_life_days     INTEGER,                         -- for expiry tracking
    min_stock_level     NUMERIC(15,4),                   -- alert threshold (not current qty!)
    max_stock_level     NUMERIC(15,4),
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_products_business_sku UNIQUE (business_id, sku)
);

CREATE INDEX idx_products_business_id ON products (business_id);
CREATE INDEX idx_products_category ON products (business_id, category_id);
CREATE INDEX idx_products_active ON products (business_id, is_active) WHERE deleted_at IS NULL;
CREATE INDEX idx_products_type ON products (business_id, product_type);
CREATE INDEX idx_products_local_id ON products (business_id, local_id) WHERE local_id IS NOT NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- product_variations
-- WHY: Size/color/flavor variants. Primary stock + pricing granularity.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE product_variations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,           -- e.g. "500g", "Red / Large"
    sku                 VARCHAR(100),
    unit_id             UUID REFERENCES units(id) ON DELETE RESTRICT,  -- override product unit
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    weight_grams        NUMERIC(15,4),                   -- for weight-based billing
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_variations_business_sku UNIQUE (business_id, sku)
);

CREATE INDEX idx_product_variations_business_id ON product_variations (business_id);
CREATE INDEX idx_product_variations_product ON product_variations (business_id, product_id);
CREATE INDEX idx_product_variations_active ON product_variations (business_id, product_id, is_active)
    WHERE deleted_at IS NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- product_locations
-- WHY: Per-branch visibility, reorder levels, and location-specific settings.
--      Does NOT store current_qty — derive from stock_movements.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE product_locations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE CASCADE,
    is_available        BOOLEAN NOT NULL DEFAULT TRUE,   -- sell at this branch?
    min_stock_level     NUMERIC(15,4),
    max_stock_level     NUMERIC(15,4),
    bin_location        VARCHAR(50),                     -- shelf/rack label
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_product_locations UNIQUE (business_id, branch_id, product_id, variation_id)
);

CREATE INDEX idx_product_locations_business_id ON product_locations (business_id);
CREATE INDEX idx_product_locations_branch ON product_locations (business_id, branch_id);
CREATE INDEX idx_product_locations_product ON product_locations (business_id, product_id, variation_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- barcodes
-- WHY: Multiple barcodes per variation (EAN, internal, supplier codes).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE barcodes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE CASCADE,
    barcode             VARCHAR(100) NOT NULL,
    barcode_type        VARCHAR(20) NOT NULL DEFAULT 'EAN13',  -- EAN13, CODE128, INTERNAL
    is_primary          BOOLEAN NOT NULL DEFAULT FALSE,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_barcodes_business_code UNIQUE (business_id, barcode)
);

CREATE INDEX idx_barcodes_business_id ON barcodes (business_id);
CREATE INDEX idx_barcodes_lookup ON barcodes (business_id, barcode) WHERE deleted_at IS NULL;
CREATE INDEX idx_barcodes_variation ON barcodes (business_id, variation_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- price_lists
-- WHY: Named price tiers (retail, wholesale, dine-in, delivery).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE price_lists (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    list_type           price_list_type_enum NOT NULL DEFAULT 'retail',
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    valid_from          TIMESTAMPTZ,
    valid_to            TIMESTAMPTZ,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_price_lists_business_name UNIQUE (business_id, name)
);

CREATE INDEX idx_price_lists_business_id ON price_lists (business_id);
CREATE INDEX idx_price_lists_default ON price_lists (business_id, is_default) WHERE is_active = TRUE;


-- ─────────────────────────────────────────────────────────────────────────────
-- price_list_items
-- WHY: Unit selling price per variation per price list.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE price_list_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    price_list_id       UUID NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE CASCADE,
    unit_price          NUMERIC(15,2) NOT NULL,
    min_qty             NUMERIC(15,4) NOT NULL DEFAULT 1,  -- tier: price applies from this qty
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_price_list_items UNIQUE (business_id, price_list_id, product_id, variation_id, min_qty),
    CONSTRAINT chk_price_list_items_price CHECK (unit_price >= 0),
    CONSTRAINT chk_price_list_items_qty CHECK (min_qty > 0)
);

CREATE INDEX idx_price_list_items_business_id ON price_list_items (business_id);
CREATE INDEX idx_price_list_items_list ON price_list_items (business_id, price_list_id);
CREATE INDEX idx_price_list_items_product ON price_list_items (business_id, product_id, variation_id);

-- Relationship notes (GROUP 2):
-- categories belongs to businesses; self-ref parent_id; has many products
-- brands belongs to businesses; has many products
-- units belongs to businesses; has many unit_conversions, products
-- products belongs to businesses, categories, brands, units; has many variations, barcodes
-- product_variations belongs to products; has many stock_movements, price_list_items
-- product_locations belongs to branch + product + variation
-- barcodes belongs to product + variation
-- price_lists belongs to businesses; has many price_list_items
-- price_list_items belongs to price_lists + product + variation
