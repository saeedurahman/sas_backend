-- Default permission catalog (global, idempotent)
INSERT INTO permissions (id, permission_key, module, description)
VALUES
    (gen_random_uuid(), 'sales.create',      'sales',      'Create and complete sales'),
    (gen_random_uuid(), 'sales.view',        'sales',      'View sales and receipts'),
    (gen_random_uuid(), 'inventory.adjust',  'inventory',  'Stock adjustments and transfers'),
    (gen_random_uuid(), 'inventory.view',    'inventory',  'View stock levels and movements'),
    (gen_random_uuid(), 'products.manage',   'products',   'Manage products, prices, and catalog'),
    (gen_random_uuid(), 'reports.view',      'reports',    'View business reports'),
    (gen_random_uuid(), 'settings.manage',   'settings',   'Manage business and app settings'),
    (gen_random_uuid(), 'users.manage',      'users',      'Manage users and roles')
ON CONFLICT (permission_key) DO UPDATE SET
    module      = EXCLUDED.module,
    description = EXCLUDED.description,
    updated_at  = NOW();
