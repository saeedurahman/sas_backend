-- Accounting module permissions (idempotent)
INSERT INTO permissions (id, permission_key, module, description)
VALUES
    (
        gen_random_uuid(),
        'accounting.coa.view',
        'accounting',
        'View chart of accounts'
    ),
    (
        gen_random_uuid(),
        'accounting.coa.manage',
        'accounting',
        'Manage chart of accounts — create, update, deactivate accounts'
    ),
    (
        gen_random_uuid(),
        'accounting.journal.view',
        'accounting',
        'View journal entries and lines'
    ),
    (
        gen_random_uuid(),
        'accounting.journal.create',
        'accounting',
        'Create and edit draft journal entries'
    ),
    (
        gen_random_uuid(),
        'accounting.journal.post',
        'accounting',
        'Post draft journal entries to the general ledger'
    )
ON CONFLICT (permission_key) DO NOTHING;
