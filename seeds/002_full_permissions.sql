-- Full permission catalog (global, idempotent)
-- Note: permissions table has permission_key, module, description (no name column).
INSERT INTO permissions (id, permission_key, module, description)
VALUES
    -- AUTH
    (gen_random_uuid(), 'auth.login',           'auth',          'Login — Authenticate and obtain access tokens'),
    (gen_random_uuid(), 'auth.logout',          'auth',          'Logout — End the current user session'),
    (gen_random_uuid(), 'auth.refresh',         'auth',          'Refresh Token — Renew access tokens using a refresh token'),

    -- PRODUCTS
    (gen_random_uuid(), 'products.view',              'products', 'View Products — Browse and search the product catalog'),
    (gen_random_uuid(), 'products.create',          'products', 'Create Products — Add new products and variations'),
    (gen_random_uuid(), 'products.update',          'products', 'Update Products — Edit product details and variations'),
    (gen_random_uuid(), 'products.delete',          'products', 'Delete Products — Soft-delete products from the catalog'),
    (gen_random_uuid(), 'products.manage_categories', 'products', 'Manage Categories — Create and organize product categories'),
    (gen_random_uuid(), 'products.manage_brands',     'products', 'Manage Brands — Create and manage product brands'),
    (gen_random_uuid(), 'products.manage_units',      'products', 'Manage Units — Define units of measure and conversions'),
    (gen_random_uuid(), 'products.manage_prices',     'products', 'Manage Prices — Set and update price list items'),
    (gen_random_uuid(), 'products.manage_barcodes',   'products', 'Manage Barcodes — Assign and update product barcodes'),

    -- INVENTORY
    (gen_random_uuid(), 'inventory.view',                    'inventory', 'View Inventory — View stock levels and movement history'),
    (gen_random_uuid(), 'inventory.adjust',                  'inventory', 'Adjust Stock — Create stock adjustments'),
    (gen_random_uuid(), 'inventory.purchase_orders.view',    'inventory', 'View Purchase Orders — List and view purchase orders'),
    (gen_random_uuid(), 'inventory.purchase_orders.create',  'inventory', 'Create Purchase Orders — Create and edit draft purchase orders'),
    (gen_random_uuid(), 'inventory.purchase_orders.receive', 'inventory', 'Receive Purchase Orders — Record goods received against POs'),
    (gen_random_uuid(), 'inventory.transfers.view',          'inventory', 'View Transfers — View inter-branch stock transfers'),
    (gen_random_uuid(), 'inventory.transfers.create',        'inventory', 'Create Transfers — Initiate stock transfers between branches'),
    (gen_random_uuid(), 'inventory.transfers.receive',       'inventory', 'Receive Transfers — Confirm receipt of transferred stock'),
    (gen_random_uuid(), 'inventory.waste.view',              'inventory', 'View Waste — View waste and shrinkage entries'),
    (gen_random_uuid(), 'inventory.waste.create',            'inventory', 'Record Waste — Log waste and shrinkage events'),

    -- SALES
    (gen_random_uuid(), 'sales.view',             'sales', 'View Sales — View sales transactions and receipts'),
    (gen_random_uuid(), 'sales.create',           'sales', 'Create Sales — Create and complete sales transactions'),
    (gen_random_uuid(), 'sales.cancel',           'sales', 'Cancel Sales — Void or cancel completed sales'),
    (gen_random_uuid(), 'sales.apply_discount',   'sales', 'Apply Discount — Apply discounts to sale lines'),
    (gen_random_uuid(), 'sales.override_price',   'sales', 'Override Price — Override unit prices at point of sale'),
    (gen_random_uuid(), 'sales.returns.view',     'sales', 'View Returns — View sale return records'),
    (gen_random_uuid(), 'sales.returns.create',   'sales', 'Create Returns — Process customer returns and refunds'),
    (gen_random_uuid(), 'sales.payments.view',    'sales', 'View Payments — View sale payment records'),

    -- CUSTOMERS
    (gen_random_uuid(), 'customers.view',         'customers', 'View Customers — Browse customer profiles'),
    (gen_random_uuid(), 'customers.create',       'customers', 'Create Customers — Add new customer records'),
    (gen_random_uuid(), 'customers.update',       'customers', 'Update Customers — Edit customer details'),
    (gen_random_uuid(), 'customers.ledger.view',  'customers', 'View Customer Ledger — View customer account balances and history'),

    -- SUPPLIERS
    (gen_random_uuid(), 'suppliers.view',              'suppliers', 'View Suppliers — Browse supplier profiles'),
    (gen_random_uuid(), 'suppliers.create',          'suppliers', 'Create Suppliers — Add new supplier records'),
    (gen_random_uuid(), 'suppliers.update',          'suppliers', 'Update Suppliers — Edit supplier details'),
    (gen_random_uuid(), 'suppliers.ledger.view',     'suppliers', 'View Supplier Ledger — View supplier account balances'),
    (gen_random_uuid(), 'suppliers.ledger.payment',  'suppliers', 'Record Supplier Payment — Post supplier payments to ledger'),

    -- EXPENSES
    (gen_random_uuid(), 'expenses.view',              'expenses', 'View Expenses — Browse expense records'),
    (gen_random_uuid(), 'expenses.create',          'expenses', 'Create Expenses — Record new business expenses'),
    (gen_random_uuid(), 'expenses.update',          'expenses', 'Update Expenses — Edit existing expense records'),
    (gen_random_uuid(), 'expenses.delete',          'expenses', 'Delete Expenses — Soft-delete expense records'),
    (gen_random_uuid(), 'expenses.categories.manage','expenses', 'Manage Expense Categories — Organize expense categories'),

    -- REGISTER
    (gen_random_uuid(), 'registers.view',        'register', 'View Registers — View cash register terminals'),
    (gen_random_uuid(), 'registers.manage',      'register', 'Manage Registers — Create and configure cash registers'),
    (gen_random_uuid(), 'shifts.view',           'register', 'View Shifts — View register shift history'),
    (gen_random_uuid(), 'shifts.open',           'register', 'Open Shift — Open a register shift with opening float'),
    (gen_random_uuid(), 'shifts.close',          'register', 'Close Shift — Close a shift with cash count'),
    (gen_random_uuid(), 'shifts.cash_movement',  'register', 'Cash Movement — Record cash in/out during a shift'),

    -- REPORTS
    (gen_random_uuid(), 'reports.view',          'reports', 'View Reports — Access the reports dashboard'),
    (gen_random_uuid(), 'reports.sales',         'reports', 'Sales Reports — View sales summaries and trends'),
    (gen_random_uuid(), 'reports.inventory',     'reports', 'Inventory Reports — View stock valuation and alerts'),
    (gen_random_uuid(), 'reports.financial',     'reports', 'Financial Reports — View profit and loss statements'),
    (gen_random_uuid(), 'reports.analytics',     'reports', 'Analytics — Access advanced analytics endpoints'),
    (gen_random_uuid(), 'reports.export',        'reports', 'Export Reports — Export sales and inventory data'),
    (gen_random_uuid(), 'reports.fraud_alerts',  'reports', 'Fraud Alerts — View cashier fraud detection alerts'),

    -- SETTINGS
    (gen_random_uuid(), 'settings.view',   'settings', 'View Settings — View business and branch settings'),
    (gen_random_uuid(), 'settings.manage', 'settings', 'Manage Settings — Update business and app configuration'),

    -- USERS
    (gen_random_uuid(), 'users.view',         'users', 'View Users — Browse user accounts'),
    (gen_random_uuid(), 'users.create',       'users', 'Create Users — Add new user accounts'),
    (gen_random_uuid(), 'users.update',       'users', 'Update Users — Edit user profiles and status'),
    (gen_random_uuid(), 'users.delete',       'users', 'Delete Users — Deactivate or remove user accounts'),
    (gen_random_uuid(), 'users.roles.manage', 'users', 'Manage Roles — Assign roles and permissions to users'),

    -- NOTIFICATIONS
    (gen_random_uuid(), 'notifications.view',   'notifications', 'View Notifications — Read in-app notifications'),
    (gen_random_uuid(), 'notifications.manage', 'notifications', 'Manage Notifications — Run alert checks and manage notifications')
ON CONFLICT (permission_key) DO NOTHING;
