-- Manufacturing module permissions (idempotent)
INSERT INTO permissions (id, permission_key, module, description)
VALUES
    (
        gen_random_uuid(),
        'manufacturing.bom.view',
        'manufacturing',
        'View BOMs — browse bills of materials and recipes'
    ),
    (
        gen_random_uuid(),
        'manufacturing.bom.manage',
        'manufacturing',
        'Manage BOMs — create and edit bills of materials'
    ),
    (
        gen_random_uuid(),
        'manufacturing.production.view',
        'manufacturing',
        'View production orders — browse manufacturing work orders'
    ),
    (
        gen_random_uuid(),
        'manufacturing.production.create',
        'manufacturing',
        'Create production orders — plan and start manufacturing runs'
    ),
    (
        gen_random_uuid(),
        'manufacturing.production.complete',
        'manufacturing',
        'Complete production orders — finalize output and consume stock'
    ),
    (
        gen_random_uuid(),
        'manufacturing.production.cancel',
        'manufacturing',
        'Cancel production orders — abort draft or in-progress work orders'
    )
ON CONFLICT (permission_key) DO NOTHING;
