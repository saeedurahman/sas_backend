-- Add discounts.view (idempotent)
INSERT INTO permissions (id, permission_key, module, description)
VALUES
    (
        gen_random_uuid(),
        'discounts.view',
        'discounts',
        'View discount schemes'
    )
ON CONFLICT (permission_key) DO NOTHING;
