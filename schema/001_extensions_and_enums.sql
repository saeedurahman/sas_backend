-- =============================================================================
-- Multi-Tenant SaaS POS + Inventory + Business Management System
-- PostgreSQL Schema — Part 1: Extensions & ENUM Types
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid() fallback

-- ── Tenant & subscription ──────────────────────────────────────────────────
-- business types: lookup table business_types (see 002_group1_tenant_auth.sql)

CREATE TYPE subscription_status_enum AS ENUM (
    'trial',
    'active',
    'past_due',
    'suspended',
    'cancelled'
);

CREATE TYPE subscription_plan_enum AS ENUM (
    'trial',
    'basic',
    'growth',
    'pro'
);

-- ── Offline sync ─────────────────────────────────────────────────────────────
CREATE TYPE sync_status_enum AS ENUM (
    'pending',       -- created/modified locally, not yet synced
    'synced',        -- confirmed by server
    'conflict'       -- server rejected or merge required
);

-- ── Auth ─────────────────────────────────────────────────────────────────────
CREATE TYPE user_status_enum AS ENUM (
    'active',
    'inactive',
    'locked'
);

-- ── Inventory ledger ─────────────────────────────────────────────────────────
CREATE TYPE stock_movement_type_enum AS ENUM (
    'opening',
    'purchase',
    'sale',
    'sale_return',
    'purchase_return',
    'adjustment_in',
    'adjustment_out',
    'transfer_in',
    'transfer_out',
    'production_in',
    'production_out',
    'waste'
);

-- Polymorphic source for stock_movements and ledger rows
CREATE TYPE reference_type_enum AS ENUM (
    'purchase_receipt_line',
    'purchase_line',
    'sale',
    'sale_line',
    'sale_return',
    'sale_return_line',
    'purchase_return',
    'purchase_return_line',
    'stock_adjustment',
    'stock_adjustment_line',
    'stock_transfer',
    'stock_transfer_line',
    'production_order',
    'production_line',
    'waste_entry',
    'waste_entry_line',
    'expense',
    'expense_payment',
    'sale_payment',
    'opening_balance',
    'manual'
);

-- ── Product catalog ──────────────────────────────────────────────────────────
CREATE TYPE product_type_enum AS ENUM (
    'standard',      -- simple buy/sell
    'variant',       -- has variations
    'composite',     -- bundle / kit
    'manufactured',  -- produced via BOM
    'service'        -- non-stock service item (restaurant service charge etc.)
);

CREATE TYPE tracking_type_enum AS ENUM (
    'none',
    'batch',
    'serial',
    'expiry'
);

CREATE TYPE price_list_type_enum AS ENUM (
    'retail',
    'wholesale',
    'dine_in',
    'delivery',
    'custom'
);

-- ── Purchasing ───────────────────────────────────────────────────────────────
CREATE TYPE purchase_order_status_enum AS ENUM (
    'draft',
    'ordered',
    'partial',
    'received',
    'cancelled'
);

-- ── Sales ────────────────────────────────────────────────────────────────────
CREATE TYPE sale_status_enum AS ENUM (
    'draft',
    'held',          -- parked bill
    'completed',
    'partially_paid',
    'cancelled',
    'voided'
);

CREATE TYPE sale_type_enum AS ENUM (
    'pos',
    'invoice',
    'dine_in',
    'takeaway',
    'delivery',
    'online'
);

CREATE TYPE payment_method_enum AS ENUM (
    'cash',
    'card',
    'upi',
    'bank_transfer',
    'wallet',
    'credit',        -- on-account / customer credit
    'cheque',
    'other'
);

CREATE TYPE payment_status_enum AS ENUM (
    'pending',
    'completed',
    'failed',
    'refunded'
);

CREATE TYPE discount_type_enum AS ENUM (
    'percentage',
    'fixed_amount'
);

CREATE TYPE ledger_entry_type_enum AS ENUM (
    'sale',
    'payment',
    'return',
    'opening_balance',
    'adjustment',
    'refund'
);

-- ── Manufacturing ────────────────────────────────────────────────────────────
CREATE TYPE production_order_status_enum AS ENUM (
    'draft',
    'in_progress',
    'completed',
    'cancelled'
);

-- ── Restaurant ─────────────────────────────────────────────────────────────────
CREATE TYPE table_status_enum AS ENUM (
    'available',
    'occupied',
    'reserved',
    'billing',
    'cleaning'
);

CREATE TYPE kot_status_enum AS ENUM (
    'pending',
    'preparing',
    'ready',
    'served',
    'cancelled'
);

CREATE TYPE modifier_selection_type_enum AS ENUM (
    'single',        -- pick one
    'multiple',      -- pick many
    'optional'
);

-- ── Transfers & adjustments ──────────────────────────────────────────────────
CREATE TYPE transfer_status_enum AS ENUM (
    'draft',
    'in_transit',
    'received',
    'cancelled'
);

CREATE TYPE adjustment_reason_enum AS ENUM (
    'count_correction',
    'damage',
    'theft',
    'expiry',
    'opening_balance',
    'other'
);

-- ── Cash register ──────────────────────────────────────────────────────────────
CREATE TYPE shift_status_enum AS ENUM (
    'open',
    'closed'
);

CREATE TYPE register_tx_type_enum AS ENUM (
    'sale',
    'sale_return',
    'expense',
    'cash_in',
    'cash_out',
    'opening_float',
    'closing_count'
);

-- ── Accounting ─────────────────────────────────────────────────────────────────
CREATE TYPE account_type_enum AS ENUM (
    'asset',
    'liability',
    'equity',
    'income',
    'expense'
);

CREATE TYPE account_subtype_enum AS ENUM (
    'cash',
    'bank',
    'accounts_receivable',
    'accounts_payable',
    'inventory',
    'cogs',
    'sales_revenue',
    'tax_payable',
    'other'
);

CREATE TYPE journal_entry_status_enum AS ENUM (
    'draft',
    'posted',
    'voided'
);

-- ── Notifications ────────────────────────────────────────────────────────────
CREATE TYPE notification_type_enum AS ENUM (
    'low_stock',
    'expiry_warning',
    'expiry_expired',
    'payment_due',
    'shift_reminder',
    'sync_conflict',
    'system'
);

CREATE TYPE notification_channel_enum AS ENUM (
    'in_app',
    'email',
    'sms',
    'push'
);

-- ── Audit ──────────────────────────────────────────────────────────────────────
CREATE TYPE audit_action_enum AS ENUM (
    'create',
    'update',
    'delete',
    'restore',
    'login',
    'logout',
    'sync'
);
