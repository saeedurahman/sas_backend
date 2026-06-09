-- Default business type lookup (global, idempotent)
INSERT INTO business_types (code, name, description, sort_order)
VALUES
    ('bakery',      'Bakery / Sweets',     'Roti, cakes, sweets, confectionery', 1),
    ('restaurant',  'Restaurant / Cafe',   'Dine-in, takeaway, fast food',        2),
    ('mart',        'Mart / Grocery',      'General grocery and daily items',     3),
    ('retail',      'Retail Store',        'Clothing, shoes, general retail',     4),
    ('hardware',    'Hardware Store',      'Building materials, tools',           5),
    ('pharmacy',    'Pharmacy',            'Medical store, medicines',            6),
    ('wholesale',   'Wholesale',           'Bulk distribution business',          7),
    ('electronics', 'Electronics',         'Phones, gadgets, accessories',        8),
    ('salon',       'Salon / Parlour',     'Beauty salon, barber shop',           9),
    ('other',       'Other',               'Any other business type',            99)
ON CONFLICT (code) DO UPDATE SET
    name        = EXCLUDED.name,
    description = EXCLUDED.description,
    sort_order  = EXCLUDED.sort_order,
    is_active   = TRUE,
    updated_at  = NOW();
