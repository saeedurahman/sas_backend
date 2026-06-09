from enum import Enum

from sqlalchemy.dialects.postgresql import ENUM


class ProductTypeEnum(str, Enum):
    standard = "standard"
    variant = "variant"
    composite = "composite"
    manufactured = "manufactured"
    service = "service"


class TrackingTypeEnum(str, Enum):
    none = "none"
    batch = "batch"
    serial = "serial"
    expiry = "expiry"


class PriceListTypeEnum(str, Enum):
    retail = "retail"
    wholesale = "wholesale"
    dine_in = "dine_in"
    delivery = "delivery"
    custom = "custom"


product_type_enum = ENUM(
    "standard",
    "variant",
    "composite",
    "manufactured",
    "service",
    name="product_type_enum",
    create_type=False,
)

tracking_type_enum = ENUM(
    "none",
    "batch",
    "serial",
    "expiry",
    name="tracking_type_enum",
    create_type=False,
)

price_list_type_enum = ENUM(
    "retail",
    "wholesale",
    "dine_in",
    "delivery",
    "custom",
    name="price_list_type_enum",
    create_type=False,
)

subscription_plan_enum = ENUM(
    "trial",
    "basic",
    "growth",
    "pro",
    name="subscription_plan_enum",
    create_type=False,
)

subscription_status_enum = ENUM(
    "trial",
    "active",
    "past_due",
    "suspended",
    "cancelled",
    name="subscription_status_enum",
    create_type=False,
)

sync_status_enum = ENUM(
    "pending",
    "synced",
    "conflict",
    name="sync_status_enum",
    create_type=False,
)

user_status_enum = ENUM(
    "active",
    "inactive",
    "locked",
    name="user_status_enum",
    create_type=False,
)


class StockMovementTypeEnum(str, Enum):
    opening = "opening"
    purchase = "purchase"
    sale = "sale"
    sale_return = "sale_return"
    purchase_return = "purchase_return"
    adjustment_in = "adjustment_in"
    adjustment_out = "adjustment_out"
    transfer_in = "transfer_in"
    transfer_out = "transfer_out"
    production_in = "production_in"
    production_out = "production_out"
    waste = "waste"


class ReferenceTypeEnum(str, Enum):
    purchase_receipt_line = "purchase_receipt_line"
    purchase_line = "purchase_line"
    sale = "sale"
    sale_line = "sale_line"
    sale_return = "sale_return"
    sale_return_line = "sale_return_line"
    purchase_return = "purchase_return"
    purchase_return_line = "purchase_return_line"
    stock_adjustment = "stock_adjustment"
    stock_adjustment_line = "stock_adjustment_line"
    stock_transfer = "stock_transfer"
    stock_transfer_line = "stock_transfer_line"
    production_order = "production_order"
    production_line = "production_line"
    waste_entry = "waste_entry"
    waste_entry_line = "waste_entry_line"
    opening_balance = "opening_balance"
    manual = "manual"


class PurchaseOrderStatusEnum(str, Enum):
    draft = "draft"
    ordered = "ordered"
    partial = "partial"
    received = "received"
    cancelled = "cancelled"


class AdjustmentReasonEnum(str, Enum):
    count_correction = "count_correction"
    damage = "damage"
    theft = "theft"
    expiry = "expiry"
    opening_balance = "opening_balance"
    other = "other"


class TransferStatusEnum(str, Enum):
    draft = "draft"
    in_transit = "in_transit"
    received = "received"
    cancelled = "cancelled"


stock_movement_type_enum = ENUM(
    "opening",
    "purchase",
    "sale",
    "sale_return",
    "purchase_return",
    "adjustment_in",
    "adjustment_out",
    "transfer_in",
    "transfer_out",
    "production_in",
    "production_out",
    "waste",
    name="stock_movement_type_enum",
    create_type=False,
)

reference_type_enum = ENUM(
    "purchase_receipt_line",
    "purchase_line",
    "sale",
    "sale_line",
    "sale_return",
    "sale_return_line",
    "purchase_return",
    "purchase_return_line",
    "stock_adjustment",
    "stock_adjustment_line",
    "stock_transfer",
    "stock_transfer_line",
    "production_order",
    "production_line",
    "waste_entry",
    "waste_entry_line",
    "opening_balance",
    "manual",
    name="reference_type_enum",
    create_type=False,
)

purchase_order_status_enum = ENUM(
    "draft",
    "ordered",
    "partial",
    "received",
    "cancelled",
    name="purchase_order_status_enum",
    create_type=False,
)

adjustment_reason_enum = ENUM(
    "count_correction",
    "damage",
    "theft",
    "expiry",
    "opening_balance",
    "other",
    name="adjustment_reason_enum",
    create_type=False,
)

transfer_status_enum = ENUM(
    "draft",
    "in_transit",
    "received",
    "cancelled",
    name="transfer_status_enum",
    create_type=False,
)


class SaleTypeEnum(str, Enum):
    pos = "pos"
    invoice = "invoice"
    dine_in = "dine_in"
    takeaway = "takeaway"
    delivery = "delivery"
    online = "online"


class SaleStatusEnum(str, Enum):
    draft = "draft"
    held = "held"
    completed = "completed"
    partially_paid = "partially_paid"
    cancelled = "cancelled"
    voided = "voided"


class PaymentMethodEnum(str, Enum):
    cash = "cash"
    card = "card"
    upi = "upi"
    bank_transfer = "bank_transfer"
    wallet = "wallet"
    credit = "credit"
    cheque = "cheque"
    other = "other"


class PaymentStatusEnum(str, Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    refunded = "refunded"


class DiscountTypeEnum(str, Enum):
    percentage = "percentage"
    fixed_amount = "fixed_amount"


class LedgerEntryTypeEnum(str, Enum):
    sale = "sale"
    payment = "payment"
    return_ = "return"
    opening_balance = "opening_balance"
    adjustment = "adjustment"
    refund = "refund"


sale_type_enum = ENUM(
    "pos",
    "invoice",
    "dine_in",
    "takeaway",
    "delivery",
    "online",
    name="sale_type_enum",
    create_type=False,
)

sale_status_enum = ENUM(
    "draft",
    "held",
    "completed",
    "partially_paid",
    "cancelled",
    "voided",
    name="sale_status_enum",
    create_type=False,
)

payment_method_enum = ENUM(
    "cash",
    "card",
    "upi",
    "bank_transfer",
    "wallet",
    "credit",
    "cheque",
    "other",
    name="payment_method_enum",
    create_type=False,
)

payment_status_enum = ENUM(
    "pending",
    "completed",
    "failed",
    "refunded",
    name="payment_status_enum",
    create_type=False,
)

discount_type_enum = ENUM(
    "percentage",
    "fixed_amount",
    name="discount_type_enum",
    create_type=False,
)

ledger_entry_type_enum = ENUM(
    "sale",
    "payment",
    "return",
    "opening_balance",
    "adjustment",
    "refund",
    name="ledger_entry_type_enum",
    create_type=False,
)


class ShiftStatusEnum(str, Enum):
    open = "open"
    closed = "closed"


class RegisterTxTypeEnum(str, Enum):
    sale = "sale"
    sale_return = "sale_return"
    expense = "expense"
    cash_in = "cash_in"
    cash_out = "cash_out"
    opening_float = "opening_float"
    closing_count = "closing_count"


shift_status_enum = ENUM(
    "open",
    "closed",
    name="shift_status_enum",
    create_type=False,
)

register_tx_type_enum = ENUM(
    "sale",
    "sale_return",
    "expense",
    "cash_in",
    "cash_out",
    "opening_float",
    "closing_count",
    name="register_tx_type_enum",
    create_type=False,
)


class NotificationTypeEnum(str, Enum):
    low_stock = "low_stock"
    expiry_warning = "expiry_warning"
    expiry_expired = "expiry_expired"
    payment_due = "payment_due"
    shift_reminder = "shift_reminder"
    sync_conflict = "sync_conflict"
    system = "system"


class NotificationChannelEnum(str, Enum):
    in_app = "in_app"
    email = "email"
    sms = "sms"
    push = "push"


notification_type_enum = ENUM(
    "low_stock",
    "expiry_warning",
    "expiry_expired",
    "payment_due",
    "shift_reminder",
    "sync_conflict",
    "system",
    name="notification_type_enum",
    create_type=False,
)

notification_channel_enum = ENUM(
    "in_app",
    "email",
    "sms",
    "push",
    name="notification_channel_enum",
    create_type=False,
)


class AuditActionEnum(str, Enum):
    create = "create"
    update = "update"
    delete = "delete"
    restore = "restore"
    login = "login"
    logout = "logout"
    sync = "sync"


audit_action_enum = ENUM(
    "create",
    "update",
    "delete",
    "restore",
    "login",
    "logout",
    "sync",
    name="audit_action_enum",
    create_type=False,
)
