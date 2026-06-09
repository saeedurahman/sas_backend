from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AnalyticsBaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DateRangeParams(AnalyticsBaseSchema):
    date_from: date | None = None
    date_to: date | None = None
    branch_id: UUID | None = None


class SalesSummaryResponse(AnalyticsBaseSchema):
    period_start: date
    period_end: date
    total_revenue: Decimal
    total_cost: Decimal
    gross_profit: Decimal
    gross_margin_pct: Decimal
    total_transactions: int
    avg_order_value: Decimal
    total_discount: Decimal
    total_tax: Decimal
    total_returns: Decimal
    net_revenue: Decimal


class PaymentBreakdownItem(AnalyticsBaseSchema):
    payment_method: str
    total_amount: Decimal
    transaction_count: int
    percentage: Decimal


class SalesTrendItem(AnalyticsBaseSchema):
    period: str
    revenue: Decimal
    transactions: int
    avg_order_value: Decimal


class TopProductItem(AnalyticsBaseSchema):
    product_id: UUID
    product_name: str
    variation_name: str | None = None
    total_qty_sold: Decimal
    total_revenue: Decimal
    total_cost: Decimal
    gross_profit: Decimal
    rank: int


class CategoryPerformanceItem(AnalyticsBaseSchema):
    category_id: UUID
    category_name: str
    total_revenue: Decimal
    total_transactions: int
    percentage_of_total: Decimal


class BranchComparisonItem(AnalyticsBaseSchema):
    branch_id: UUID
    branch_name: str
    total_revenue: Decimal
    total_transactions: int
    avg_order_value: Decimal
    total_profit: Decimal


class CashierPerformanceItem(AnalyticsBaseSchema):
    user_id: UUID
    user_name: str
    total_sales: int
    total_revenue: Decimal
    total_returns: int
    total_voids: int
    total_discounts: Decimal
    avg_order_value: Decimal
    rank: int


class FraudAlertItem(AnalyticsBaseSchema):
    alert_type: str
    user_id: UUID
    user_name: str
    branch_id: UUID
    count: int
    total_amount: Decimal
    severity: str
    description: str


class StockValuationItem(AnalyticsBaseSchema):
    product_id: UUID
    product_name: str
    variation_id: UUID | None = None
    variation_name: str | None = None
    branch_id: UUID
    branch_name: str
    current_qty: Decimal
    avg_cost: Decimal
    total_value: Decimal


class LowStockAlertItem(AnalyticsBaseSchema):
    product_id: UUID
    product_name: str
    variation_id: UUID | None = None
    branch_id: UUID
    branch_name: str
    current_qty: Decimal
    min_stock_level: Decimal
    shortage: Decimal


class InventoryInsightsResponse(AnalyticsBaseSchema):
    total_products: int
    total_stock_value: Decimal
    low_stock_count: int
    out_of_stock_count: int
    low_stock_items: list[LowStockAlertItem] = Field(default_factory=list)


class CustomerInsightItem(AnalyticsBaseSchema):
    customer_id: UUID
    customer_name: str
    phone: str | None = None
    total_purchases: int
    total_spent: Decimal
    outstanding_balance: Decimal
    last_purchase_date: date | None = None
    avg_order_value: Decimal
    days_since_last_purchase: int | None = None


class ExpenseSummaryItem(AnalyticsBaseSchema):
    category_id: UUID
    category_name: str
    total_amount: Decimal
    transaction_count: int
    percentage_of_total: Decimal


class ProfitLossResponse(AnalyticsBaseSchema):
    period_start: date
    period_end: date
    total_revenue: Decimal
    total_cogs: Decimal
    gross_profit: Decimal
    gross_margin_pct: Decimal
    total_expenses: Decimal
    net_profit: Decimal
    net_margin_pct: Decimal
    expense_breakdown: list[ExpenseSummaryItem] = Field(default_factory=list)


class DashboardSummaryResponse(AnalyticsBaseSchema):
    today_revenue: Decimal
    today_transactions: int
    today_profit: Decimal
    today_avg_order: Decimal
    this_month_revenue: Decimal
    this_month_profit: Decimal
    low_stock_alerts: int
    open_shifts: int
    pending_customer_balances: Decimal
    pending_supplier_balances: Decimal


class TodayVsYesterdayResponse(AnalyticsBaseSchema):
    today_revenue: Decimal
    yesterday_revenue: Decimal
    revenue_change_pct: Decimal
    today_transactions: int
    yesterday_transactions: int
    transaction_change_pct: Decimal
    today_profit: Decimal
    yesterday_profit: Decimal
    profit_change_pct: Decimal
    today_avg_order: Decimal
    yesterday_avg_order: Decimal


class PeakHourItem(AnalyticsBaseSchema):
    hour: int
    hour_label: str
    total_revenue: Decimal
    transaction_count: int
    avg_order_value: Decimal


class PeakDayItem(AnalyticsBaseSchema):
    day_of_week: int
    day_name: str
    total_revenue: Decimal
    transaction_count: int
    avg_order_value: Decimal


class ProductMovementItem(AnalyticsBaseSchema):
    product_id: UUID
    product_name: str
    variation_name: str | None = None
    total_qty_sold: Decimal
    total_revenue: Decimal
    last_sale_date: date | None = None
    days_since_last_sale: int | None = None
    movement_category: str


class DeadStockItem(AnalyticsBaseSchema):
    product_id: UUID
    product_name: str
    variation_name: str | None = None
    branch_id: UUID
    branch_name: str
    current_qty: Decimal
    last_sale_date: date | None = None
    days_since_last_sale: int
    stock_value: Decimal


class TaxByRateItem(AnalyticsBaseSchema):
    tax_rate: Decimal
    total_sales_amount: Decimal
    total_tax_amount: Decimal
    transaction_count: int


class TaxSummaryResponse(AnalyticsBaseSchema):
    period_start: date
    period_end: date
    total_taxable_sales: Decimal
    total_tax_collected: Decimal
    tax_by_rate: list[TaxByRateItem]
    total_taxable_purchases: Decimal
    total_tax_paid: Decimal


class RecentTransactionItem(AnalyticsBaseSchema):
    sale_id: UUID
    sale_number: str
    sale_type: str
    status: str
    sold_at: datetime
    branch_name: str
    customer_name: str | None = None
    cashier_name: str
    total_amount: Decimal
    payment_methods: list[str]


class CashInDrawerResponse(AnalyticsBaseSchema):
    has_active_shift: bool
    shift_id: UUID | None = None
    cash_register_name: str | None = None
    opening_float: Decimal
    cash_sales_total: Decimal
    cash_returns_total: Decimal
    cash_in_total: Decimal
    cash_out_total: Decimal
    cash_expenses_total: Decimal
    expected_cash: Decimal
    branch_id: UUID | None = None


class EnhancedDashboardResponse(AnalyticsBaseSchema):
    today_revenue: Decimal
    today_transactions: int
    today_profit: Decimal
    today_avg_order: Decimal
    today_expenses: Decimal
    this_month_revenue: Decimal
    this_month_profit: Decimal
    revenue_vs_yesterday_pct: Decimal
    low_stock_alerts: int
    out_of_stock_count: int
    open_shifts: int
    pending_customer_receivables: Decimal
    pending_supplier_payables: Decimal
    cash_in_drawer: Decimal
    recent_transactions: list[RecentTransactionItem] = Field(default_factory=list)
