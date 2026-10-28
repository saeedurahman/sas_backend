"""Analytics and invoice export permission enforcement (migrated from test_batch6)."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant

pytestmark = pytest.mark.integration


def test_reports_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch6 {unique_suffix}",
        owner_name="Batch6 Owner",
        phone_prefix="360",
    )
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    today = date.today()
    week_ago = today - timedelta(days=7)
    date_q = f"date_from={week_ago.isoformat()}&date_to={today.isoformat()}"
    assert manager_headers is not None and cashier_headers is not None

    read_cases = [
        (
            "GET /analytics/dashboard (manager)",
            manager_headers,
            f"{api_base}/analytics/dashboard",
            200,
        ),
        (
            "GET /analytics/dashboard (cashier, denied)",
            cashier_headers,
            f"{api_base}/analytics/dashboard",
            403,
        ),
        (
            "GET /analytics/sales-summary (manager)",
            manager_headers,
            f"{api_base}/analytics/sales-summary?{date_q}",
            200,
        ),
        (
            "GET /analytics/sales-summary (cashier, denied)",
            cashier_headers,
            f"{api_base}/analytics/sales-summary?{date_q}",
            403,
        ),
        (
            "GET /analytics/stock-valuation (manager)",
            manager_headers,
            f"{api_base}/analytics/stock-valuation",
            200,
        ),
        (
            "GET /analytics/profit-loss (manager)",
            manager_headers,
            f"{api_base}/analytics/profit-loss?{date_q}",
            200,
        ),
        (
            "GET /analytics/customer-insights (manager)",
            manager_headers,
            f"{api_base}/analytics/customer-insights",
            200,
        ),
        (
            "GET /analytics/fraud-alerts (owner)",
            owner_headers,
            f"{api_base}/analytics/fraud-alerts?{date_q}",
            200,
        ),
        (
            "GET /analytics/fraud-alerts (manager, denied)",
            manager_headers,
            f"{api_base}/analytics/fraud-alerts?{date_q}",
            403,
        ),
        (
            "GET /invoice/export/sales (manager)",
            manager_headers,
            f"{api_base}/invoice/export/sales?{date_q}&format=csv",
            200,
        ),
        (
            "GET /invoice/export/sales (cashier, denied)",
            cashier_headers,
            f"{api_base}/invoice/export/sales?{date_q}&format=csv",
            403,
        ),
        (
            "GET /invoice/export/inventory (manager)",
            manager_headers,
            f"{api_base}/invoice/export/inventory?format=csv",
            200,
        ),
        (
            "GET /invoice/export/inventory (cashier, denied)",
            cashier_headers,
            f"{api_base}/invoice/export/inventory?format=csv",
            403,
        ),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)
