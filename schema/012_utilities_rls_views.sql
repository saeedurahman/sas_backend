-- =============================================================================
-- Utilities: triggers, RLS, stock balance materialized view
-- =============================================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION current_business_id()
RETURNS UUID AS $$
BEGIN
    RETURN NULLIF(current_setting('app.current_business_id', TRUE), '')::UUID;
EXCEPTION
    WHEN OTHERS THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION current_business_id IS
    'SET LOCAL app.current_business_id = ''<uuid>'' before tenant queries.';

-- updated_at triggers
CREATE TRIGGER trg_businesses_updated_at BEFORE UPDATE ON businesses
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_products_updated_at BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_stock_movements_updated_at BEFORE UPDATE ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_sales_updated_at BEFORE UPDATE ON sales
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
-- (Add remaining tables via migration generator in CI)


-- ── Row-Level Security ───────────────────────────────────────────────────────
ALTER TABLE businesses ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_businesses ON businesses
    USING (id = current_business_id())
    WITH CHECK (id = current_business_id());

-- Macro pattern for all business_id-scoped tables:
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_name = c.table_name AND t.table_schema = c.table_schema
        WHERE c.table_schema = 'public'
          AND c.column_name = 'business_id'
          AND t.table_type = 'BASE TABLE'
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I
             USING (business_id = current_business_id())
             WITH CHECK (business_id = current_business_id())',
            tbl
        );
    END LOOP;
END $$;

-- permissions: global read-only catalog
ALTER TABLE permissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY permissions_read_all ON permissions FOR SELECT USING (TRUE);


-- ── Stock balance materialized view [DENORMALIZED] ───────────────────────────
CREATE MATERIALIZED VIEW mv_stock_balances AS
SELECT
    sm.business_id,
    sm.branch_id,
    sm.product_id,
    sm.variation_id,
    SUM(sm.qty)                    AS on_hand_qty,
    SUM(sm.qty * sm.cost_per_unit) AS inventory_value,
    MAX(sm.movement_at)            AS last_movement_at
FROM stock_movements sm
WHERE sm.deleted_at IS NULL
GROUP BY sm.business_id, sm.branch_id, sm.product_id, sm.variation_id;

CREATE UNIQUE INDEX idx_mv_stock_balances_pk
    ON mv_stock_balances (business_id, branch_id, product_id, variation_id);

CREATE INDEX idx_mv_stock_balances_business
    ON mv_stock_balances (business_id, branch_id);


CREATE VIEW v_fifo_layers AS
SELECT
    pl.business_id,
    po.branch_id,
    pl.product_id,
    pl.variation_id,
    pl.id            AS purchase_line_id,
    pl.cost_per_unit,
    pl.qty_remaining,
    pl.received_qty,
    pl.created_at    AS layer_date,
    pl.batch_number,
    pl.expiry_date
FROM purchase_lines pl
JOIN purchase_orders po ON po.id = pl.purchase_order_id
WHERE pl.deleted_at IS NULL AND pl.qty_remaining > 0;
