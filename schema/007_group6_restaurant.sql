-- =============================================================================
-- GROUP 6 — RESTAURANT SPECIFIC
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- floor_plans
-- WHY: Dining area layout per branch (Main Hall, Terrace, AC Section).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE floor_plans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    layout_json         JSONB NOT NULL DEFAULT '{}',     -- optional visual layout metadata
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_floor_plans_name UNIQUE (business_id, branch_id, name)
);

CREATE INDEX idx_floor_plans_business_id ON floor_plans (business_id);
CREATE INDEX idx_floor_plans_branch ON floor_plans (business_id, branch_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- tables
-- WHY: Dine-in tables per branch/floor. Linked to active sales.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE tables (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    floor_plan_id       UUID REFERENCES floor_plans(id) ON DELETE SET NULL,
    table_number        VARCHAR(20) NOT NULL,
    capacity            INTEGER NOT NULL DEFAULT 4,
    status              table_status_enum NOT NULL DEFAULT 'available',
    pos_x               NUMERIC(8,2),                    -- floor plan coordinates
    pos_y               NUMERIC(8,2),
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
    CONSTRAINT uq_tables_number UNIQUE (business_id, branch_id, table_number),
    CONSTRAINT chk_tables_capacity CHECK (capacity > 0)
);

CREATE INDEX idx_tables_business_id ON tables (business_id);
CREATE INDEX idx_tables_branch_status ON tables (business_id, branch_id, status);
CREATE INDEX idx_tables_floor ON tables (business_id, floor_plan_id);

ALTER TABLE sales
    ADD CONSTRAINT fk_sales_table
    FOREIGN KEY (table_id) REFERENCES tables(id) ON DELETE SET NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- modifier_groups
-- WHY: Groups modifiers (Toppings, Spice Level, Cooking Preference).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE modifier_groups (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    selection_type      modifier_selection_type_enum NOT NULL DEFAULT 'multiple',
    min_selections      INTEGER NOT NULL DEFAULT 0,
    max_selections      INTEGER,                         -- null = unlimited
    is_required         BOOLEAN NOT NULL DEFAULT FALSE,
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
    CONSTRAINT uq_modifier_groups_name UNIQUE (business_id, name)
);

CREATE INDEX idx_modifier_groups_business_id ON modifier_groups (business_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- modifiers
-- WHY: Individual add-ons (Extra Cheese, No Onion) with optional price delta.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE modifiers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    modifier_group_id   UUID NOT NULL REFERENCES modifier_groups(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    price_delta         NUMERIC(15,2) NOT NULL DEFAULT 0, -- added to line item price
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'synced',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    deleted_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_modifiers_name UNIQUE (business_id, modifier_group_id, name)
);

CREATE INDEX idx_modifiers_business_id ON modifiers (business_id);
CREATE INDEX idx_modifiers_group ON modifiers (business_id, modifier_group_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- product_modifier_groups
-- WHY: Links products to applicable modifier groups (M:N junction).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE product_modifier_groups (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    modifier_group_id   UUID NOT NULL REFERENCES modifier_groups(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_product_modifier_groups UNIQUE (business_id, product_id, modifier_group_id)
);

CREATE INDEX idx_product_modifier_groups_business ON product_modifier_groups (business_id);
CREATE INDEX idx_product_modifier_groups_product ON product_modifier_groups (business_id, product_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- kot_orders
-- WHY: Kitchen Order Ticket header — sent to kitchen display.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE kot_orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    sale_id             UUID REFERENCES sales(id) ON DELETE SET NULL,
    table_id            UUID REFERENCES tables(id) ON DELETE SET NULL,
    kot_number          VARCHAR(50) NOT NULL,
    status              kot_status_enum NOT NULL DEFAULT 'pending',
    fired_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(), -- sent to kitchen
    ready_at            TIMESTAMPTZ,
    served_at           TIMESTAMPTZ,
    notes               TEXT,
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_kot_orders_number UNIQUE (business_id, kot_number)
);

CREATE INDEX idx_kot_orders_business_id ON kot_orders (business_id);
CREATE INDEX idx_kot_orders_branch_status ON kot_orders (business_id, branch_id, status, fired_at);
CREATE INDEX idx_kot_orders_sale ON kot_orders (business_id, sale_id);
CREATE INDEX idx_kot_orders_table ON kot_orders (business_id, table_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- kot_order_lines
-- WHY: Individual items on a KOT with modifiers and kitchen instructions.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE kot_order_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id         UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    kot_order_id        UUID NOT NULL REFERENCES kot_orders(id) ON DELETE CASCADE,
    sale_line_id        UUID REFERENCES sale_lines(id) ON DELETE SET NULL,
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    variation_id        UUID REFERENCES product_variations(id) ON DELETE RESTRICT,
    qty                 NUMERIC(15,4) NOT NULL,
    modifiers_json      JSONB NOT NULL DEFAULT '[]',     -- [{modifier_id, name, price_delta}]
    kitchen_notes       TEXT,
    status              kot_status_enum NOT NULL DEFAULT 'pending',
    local_id            UUID,
    server_id           UUID,
    sync_status         sync_status_enum NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    created_by          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_kot_order_lines_qty CHECK (qty > 0)
);

CREATE INDEX idx_kot_order_lines_business_id ON kot_order_lines (business_id);
CREATE INDEX idx_kot_order_lines_kot ON kot_order_lines (business_id, kot_order_id);
CREATE INDEX idx_kot_order_lines_status ON kot_order_lines (business_id, status);

-- Relationship notes (GROUP 6):
-- floor_plans belongs to branch; has many tables
-- tables belongs to branch, floor_plan; referenced by sales, kot_orders
-- modifier_groups has many modifiers; linked to products via product_modifier_groups
-- modifiers belongs to modifier_group
-- kot_orders belongs to sale, table, branch; has many kot_order_lines
-- kot_order_lines belongs to kot_order, sale_line
