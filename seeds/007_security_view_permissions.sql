-- View permissions for endpoints that previously required only a valid login (idempotent)
INSERT INTO permissions (id, permission_key, module, description)
VALUES
    (
        gen_random_uuid(),
        'roles.view',
        'users',
        'View roles and their permissions'
    ),
    (
        gen_random_uuid(),
        'permissions.view',
        'users',
        'View the permission catalog'
    ),
    (
        gen_random_uuid(),
        'branches.view',
        'users',
        'View branches'
    )
ON CONFLICT (permission_key) DO NOTHING;
