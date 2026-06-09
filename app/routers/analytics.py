from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_manager, require_owner
from app.models.user import User
from app.schemas.analytics import (
    BranchComparisonItem,
    CashInDrawerResponse,
    CashierPerformanceItem,
    CategoryPerformanceItem,
    CustomerInsightItem,
    DashboardSummaryResponse,
    DeadStockItem,
    EnhancedDashboardResponse,
    ExpenseSummaryItem,
    FraudAlertItem,
    InventoryInsightsResponse,
    PaymentBreakdownItem,
    PeakDayItem,
    PeakHourItem,
    ProductMovementItem,
    ProfitLossResponse,
    RecentTransactionItem,
    SalesSummaryResponse,
    SalesTrendItem,
    StockValuationItem,
    TaxSummaryResponse,
    TodayVsYesterdayResponse,
    TopProductItem,
)
from app.services.analytics_service import (
    get_branch_comparison,
    get_cash_in_drawer,
    get_cashier_performance,
    get_category_performance,
    get_customer_insights,
    get_dashboard_summary,
    get_dead_stock,
    get_enhanced_dashboard,
    get_expense_summary,
    get_fraud_alerts,
    get_inventory_insights,
    get_payment_breakdown,
    get_peak_days,
    get_peak_hours,
    get_product_movement,
    get_profit_loss,
    get_recent_transactions,
    get_sales_summary,
    get_sales_trend,
    get_stock_valuation,
    get_tax_summary,
    get_today_vs_yesterday,
    get_top_products,
)

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get(
    "/dashboard",
    response_model=DashboardSummaryResponse,
    status_code=status.HTTP_200_OK,
)
async def dashboard(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_dashboard_summary(
        db, current_user.business_id, branch_id
    )


@router.get(
    "/sales-summary",
    response_model=SalesSummaryResponse,
    status_code=status.HTTP_200_OK,
)
async def sales_summary(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_sales_summary(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/sales-trend",
    response_model=list[SalesTrendItem],
    status_code=status.HTTP_200_OK,
)
async def sales_trend(
    date_from: date = Query(...),
    date_to: date = Query(...),
    period: str = Query(default="daily"),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_sales_trend(
        db,
        current_user.business_id,
        period,
        date_from,
        date_to,
        branch_id,
    )


@router.get(
    "/payment-breakdown",
    response_model=list[PaymentBreakdownItem],
    status_code=status.HTTP_200_OK,
)
async def payment_breakdown(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_payment_breakdown(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/top-products",
    response_model=list[TopProductItem],
    status_code=status.HTTP_200_OK,
)
async def top_products(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_top_products(
        db,
        current_user.business_id,
        date_from,
        date_to,
        branch_id,
        limit,
    )


@router.get(
    "/category-performance",
    response_model=list[CategoryPerformanceItem],
    status_code=status.HTTP_200_OK,
)
async def category_performance(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_category_performance(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/branch-comparison",
    response_model=list[BranchComparisonItem],
    status_code=status.HTTP_200_OK,
)
async def branch_comparison(
    date_from: date = Query(...),
    date_to: date = Query(...),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_branch_comparison(
        db, current_user.business_id, date_from, date_to
    )


@router.get(
    "/cashier-performance",
    response_model=list[CashierPerformanceItem],
    status_code=status.HTTP_200_OK,
)
async def cashier_performance(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_cashier_performance(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/fraud-alerts",
    response_model=list[FraudAlertItem],
    status_code=status.HTTP_200_OK,
)
async def fraud_alerts(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await get_fraud_alerts(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/stock-valuation",
    response_model=list[StockValuationItem],
    status_code=status.HTTP_200_OK,
)
async def stock_valuation(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_stock_valuation(
        db, current_user.business_id, branch_id
    )


@router.get(
    "/inventory-insights",
    response_model=InventoryInsightsResponse,
    status_code=status.HTTP_200_OK,
)
async def inventory_insights(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_inventory_insights(
        db, current_user.business_id, branch_id
    )


@router.get(
    "/customer-insights",
    response_model=list[CustomerInsightItem],
    status_code=status.HTTP_200_OK,
)
async def customer_insights(
    limit: int = Query(default=50, ge=1, le=200),
    sort_by: str = Query(default="total_spent"),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_customer_insights(
        db, current_user.business_id, limit, sort_by
    )


@router.get(
    "/profit-loss",
    response_model=ProfitLossResponse,
    status_code=status.HTTP_200_OK,
)
async def profit_loss(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_profit_loss(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/expense-summary",
    response_model=list[ExpenseSummaryItem],
    status_code=status.HTTP_200_OK,
)
async def expense_summary(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_expense_summary(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/dashboard/enhanced",
    response_model=EnhancedDashboardResponse,
    status_code=status.HTTP_200_OK,
)
async def enhanced_dashboard(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_enhanced_dashboard(
        db, current_user.business_id, branch_id
    )


@router.get(
    "/today-vs-yesterday",
    response_model=TodayVsYesterdayResponse,
    status_code=status.HTTP_200_OK,
)
async def today_vs_yesterday(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_today_vs_yesterday(
        db, current_user.business_id, branch_id
    )


@router.get(
    "/peak-hours",
    response_model=list[PeakHourItem],
    status_code=status.HTTP_200_OK,
)
async def peak_hours(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_peak_hours(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/peak-days",
    response_model=list[PeakDayItem],
    status_code=status.HTTP_200_OK,
)
async def peak_days(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_peak_days(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/product-movement",
    response_model=list[ProductMovementItem],
    status_code=status.HTTP_200_OK,
)
async def product_movement(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    dead_stock_days: int = Query(default=30, ge=1),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_product_movement(
        db,
        current_user.business_id,
        date_from,
        date_to,
        branch_id,
        dead_stock_days,
    )


@router.get(
    "/dead-stock",
    response_model=list[DeadStockItem],
    status_code=status.HTTP_200_OK,
)
async def dead_stock(
    dead_stock_days: int = Query(default=30, ge=1),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_dead_stock(
        db, current_user.business_id, dead_stock_days, branch_id
    )


@router.get(
    "/tax-summary",
    response_model=TaxSummaryResponse,
    status_code=status.HTTP_200_OK,
)
async def tax_summary(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_tax_summary(
        db, current_user.business_id, date_from, date_to, branch_id
    )


@router.get(
    "/recent-transactions",
    response_model=list[RecentTransactionItem],
    status_code=status.HTTP_200_OK,
)
async def recent_transactions(
    branch_id: UUID | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_recent_transactions(
        db, current_user.business_id, branch_id, limit
    )


@router.get(
    "/cash-in-drawer",
    response_model=CashInDrawerResponse,
    status_code=status.HTTP_200_OK,
)
async def cash_in_drawer(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await get_cash_in_drawer(
        db, current_user.business_id, branch_id
    )
