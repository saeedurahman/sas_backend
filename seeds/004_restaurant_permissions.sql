-- Restaurant module permissions (idempotent)
INSERT INTO permissions (id, permission_key, module, description)
VALUES
    (
        gen_random_uuid(),
        'restaurant.floor_plans.view',
        'restaurant',
        'View floor plans — browse dining area layouts'
    ),
    (
        gen_random_uuid(),
        'restaurant.floor_plans.manage',
        'restaurant',
        'Manage floor plans — create and edit dining area layouts'
    ),
    (
        gen_random_uuid(),
        'restaurant.tables.view',
        'restaurant',
        'View tables — browse dine-in table layout and status'
    ),
    (
        gen_random_uuid(),
        'restaurant.tables.manage',
        'restaurant',
        'Manage tables — create and edit dine-in tables'
    ),
    (
        gen_random_uuid(),
        'restaurant.tables.update_status',
        'restaurant',
        'Update table status — change table availability state'
    ),
    (
        gen_random_uuid(),
        'restaurant.modifiers.view',
        'restaurant',
        'View modifiers — browse modifier groups and add-ons'
    ),
    (
        gen_random_uuid(),
        'restaurant.modifiers.manage',
        'restaurant',
        'Manage modifiers — create and edit modifier groups and add-ons'
    ),
    (
        gen_random_uuid(),
        'restaurant.kot.view',
        'restaurant',
        'View KOT — browse kitchen order tickets'
    ),
    (
        gen_random_uuid(),
        'restaurant.kot.update_status',
        'restaurant',
        'Update KOT status — mark kitchen items preparing, ready, or served'
    ),
    (
        gen_random_uuid(),
        'restaurant.kot.fire',
        'restaurant',
        'Fire to kitchen — send order items to the kitchen display'
    )
ON CONFLICT (permission_key) DO NOTHING;
