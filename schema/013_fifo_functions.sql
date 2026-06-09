-- =============================================================================
-- FIFO costing: weighted average cost from oldest purchase layers
-- =============================================================================

CREATE OR REPLACE FUNCTION get_fifo_cost(
    p_business_id   UUID,
    p_product_id  UUID,
    p_variation_id UUID,
    p_qty         NUMERIC(15,4)
)
RETURNS NUMERIC(15,2)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_remaining   NUMERIC(15,4);
    v_total_cost  NUMERIC(15,2) := 0;
    v_take        NUMERIC(15,4);
    r             RECORD;
BEGIN
    IF p_qty IS NULL OR p_qty <= 0 THEN
        RAISE EXCEPTION 'get_fifo_cost: p_qty must be positive (got %)', p_qty;
    END IF;

    v_remaining := p_qty;

    FOR r IN
        SELECT
            pl.id,
            pl.cost_per_unit,
            pl.qty_remaining
        FROM purchase_lines pl
        WHERE pl.business_id = p_business_id
          AND pl.product_id = p_product_id
          AND pl.variation_id IS NOT DISTINCT FROM p_variation_id
          AND pl.qty_remaining > 0
          AND pl.deleted_at IS NULL
        ORDER BY pl.created_at ASC
    LOOP
        v_take := LEAST(v_remaining, r.qty_remaining);
        v_total_cost := v_total_cost + (v_take * r.cost_per_unit);
        v_remaining := v_remaining - v_take;

        IF v_remaining <= 0 THEN
            EXIT;
        END IF;
    END LOOP;

    IF v_remaining > 0 THEN
        RAISE EXCEPTION
            'get_fifo_cost: insufficient FIFO stock for business=%, product=%, variation=% — need %, short by %',
            p_business_id, p_product_id, p_variation_id, p_qty, v_remaining;
    END IF;

    RETURN ROUND(v_total_cost / p_qty, 2);
END;
$$;

COMMENT ON FUNCTION get_fifo_cost IS
    'Weighted-average unit cost from oldest purchase_lines (qty_remaining > 0) for the requested qty.';
