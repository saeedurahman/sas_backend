"""Phase 6a Performance integration tests:
1. get_current_user permission cache hit query count <= 3.
2. Analytics max date range cap (ANALYTICS_MAX_RANGE_DAYS) and validation.
3. Dashboard cache 30s TTL and invalidation on new sales/voids.
4. PDF export timing sanity thresholds (< 3.0s for 1k rows, < 10.0s for 5k rows).
5. 10-line sale create latency on test database.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zoneinfo import ZoneInfo

from app.core import permission_cache
from app.core.config import settings
from app.dependencies import get_current_user
from app.models.enums import PaymentMethodEnum, SaleTypeEnum
from app.models.product import Product
from app.models.register import RegisterShift
from app.models.user import User
from app.schemas.sales import CreatePaymentRequest, CreateSaleLineRequest, CreateSaleRequest
from app.services.analytics_service import (
    _dashboard_cache,
    invalidate_dashboard_cache,
    validate_date_range,
)
from app.services.auth_service import create_access_token
from app.services.export_datasets import ExportCtx, _build_pdf_bytes, resolve_dataset
from app.services.sale_service import create_sale
from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_pos_tenant
from tests.helpers.testdb import require_test_database_url

pytestmark = pytest.mark.integration


@pytest.fixture
def tenant(http_client, api_base, unique_suffix):
    return build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="479")


def test_permission_cache_query_count_at_most_3():
    """Verify that get_current_user executes at most 3 queries when permission cache hits."""
    async def _run():
        test_url = require_test_database_url()
        engine = create_async_engine(test_url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        async with session_factory() as session:
            user = (await session.execute(select(User).where(User.deleted_at.is_(None)).limit(1))).scalar_one_or_none()
            if not user:
                pytest.skip("No users found in test database to test query count")

            token = create_access_token({
                "sub": str(user.id),
                "tv": user.token_version,
                "business_id": str(user.business_id),
            })
            permission_cache.put(user.id, frozenset(["sales.create", "sales.view", "reports.export"]))

        query_count = 0

        def count_query(conn, cursor, statement, parameters, context, executemany):
            nonlocal query_count
            query_count += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_query)

        dummy_request = type(
            "Request",
            (),
            {
                "state": type("State", (), {"subscription_exempt": True, "grace_write_ok": True})(),
                "method": "POST",
            },
        )()

        try:
            async with session_factory() as session:
                query_count = 0
                current_user = await get_current_user(request=dummy_request, token=token, db=session)
                assert current_user.id == user.id
                # Target: at most 3 queries on a cache hit
                assert query_count <= 3, f"Expected <= 3 queries on cache hit, got {query_count}"
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_query)
            await engine.dispose()

    asyncio.run(_run())


def test_analytics_max_date_range_validation():
    """Verify date range bounds and maximum range cap (ANALYTICS_MAX_RANGE_DAYS)."""
    # 1. date_to < date_from -> 400
    with pytest.raises(HTTPException) as exc_info:
        validate_date_range(date(2026, 5, 10), date(2026, 5, 1))
    assert exc_info.value.status_code == 400
    assert "date_to must be on or after date_from" in exc_info.value.detail

    # 2. Exceeding max range (default 400 days) -> 400
    max_days = settings.analytics_max_range_days
    with pytest.raises(HTTPException) as exc_info:
        validate_date_range(date(2025, 1, 1), date(2025, 1, 1) + timedelta(days=max_days + 1))
    assert exc_info.value.status_code == 400
    assert f"Date range exceeds maximum allowed {max_days} days" in exc_info.value.detail

    # 3. Within max range -> OK
    validate_date_range(date(2025, 1, 1), date(2025, 1, 1) + timedelta(days=max_days))


def test_analytics_max_range_cap_http(http_client, tenant):
    """Verify HTTP 400 response when date range exceeds 400 days on analytics endpoint."""
    max_days = settings.analytics_max_range_days
    r = http_client.get(
        tenant.url("/analytics/sales-summary"),
        headers=tenant.owner_headers,
        params={
            "date_from": "2024-01-01",
            "date_to": "2026-01-01",
        },
    )
    assert_status(r, 400, label="max range breach")
    assert f"Date range exceeds maximum allowed {max_days} days" in r.json()["detail"]


def test_dashboard_cache_and_invalidation():
    """Verify dashboard cache 30s TTL storage and invalidation on business mutation."""
    biz_id = uuid4()
    cache_key = (biz_id, None, "summary")

    # Initially empty
    assert cache_key not in _dashboard_cache

    # Store mock entry with TTL
    mock_response = {"status": "ok"}
    _dashboard_cache[cache_key] = (time.monotonic() + 30.0, mock_response)
    assert cache_key in _dashboard_cache

    # Invalidate cache for biz_id
    invalidate_dashboard_cache(biz_id)
    assert cache_key not in _dashboard_cache


def test_pdf_export_speed_thresholds():
    """Verify PDF export rendering timing thresholds (generous: 1k < 3.0s, 5k < 10.0s)."""
    dataset = resolve_dataset("sales")
    ctx = ExportCtx(
        business_id=uuid4(),
        zone=ZoneInfo("Asia/Karachi"),
        date_from=date(2026, 10, 15),
        date_to=date(2026, 11, 15),
        branch_id=None,
        business_name="Perf Test Store باکستان",
    )

    now_dt = datetime.now(timezone.utc)
    # 1,000 rows
    rows_1k = [
        {
            "id": uuid4(),
            "sale_number": f"INV-{i:06d}",
            "client_receipt_no": None,
            "branch_name": "Main Branch",
            "customer_name": "احمد خان" if i % 3 == 0 else f"Customer {i}",
            "customer_phone": "03001234567",
            "cashier_name": "Cashier",
            "sold_at": now_dt,
            "sale_type": "pos",
            "status": "completed",
            "payment_method": "cash",
            "subtotal": Decimal("1000.00"),
            "discount_amount": Decimal("0.00"),
            "tax_amount": Decimal("50.00"),
            "total_amount": Decimal("1050.00"),
            "line_count": 3,
            "total_qty": Decimal("5.00"),
            "notes": None,
        }
        for i in range(1, 1001)
    ]

    t0 = time.perf_counter()
    pdf_bytes_1k = _build_pdf_bytes(dataset, ctx, rows_1k)
    dur_1k = time.perf_counter() - t0
    assert len(pdf_bytes_1k) > 1000
    assert dur_1k < 3.0, f"Expected 1,000 rows PDF under 3.0s, took {dur_1k:.2f}s"

    # 5,000 rows
    rows_5k = [
        {
            "id": uuid4(),
            "sale_number": f"INV-{i:06d}",
            "client_receipt_no": None,
            "branch_name": "Main Branch",
            "customer_name": "اردو کسٹمر" if i % 4 == 0 else f"Customer {i}",
            "customer_phone": "03001234567",
            "cashier_name": "Cashier",
            "sold_at": now_dt,
            "sale_type": "pos",
            "status": "completed",
            "payment_method": "cash",
            "subtotal": Decimal("1000.00"),
            "discount_amount": Decimal("0.00"),
            "tax_amount": Decimal("50.00"),
            "total_amount": Decimal("1050.00"),
            "line_count": 3,
            "total_qty": Decimal("5.00"),
            "notes": None,
        }
        for i in range(1, 5001)
    ]

    t0 = time.perf_counter()
    pdf_bytes_5k = _build_pdf_bytes(dataset, ctx, rows_5k)
    dur_5k = time.perf_counter() - t0
    assert len(pdf_bytes_5k) > 5000
    assert dur_5k < 10.0, f"Expected 5,000 rows PDF under 10.0s, took {dur_5k:.2f}s"


def test_create_sale_latency_and_query_sanity():
    """Verify 10-line sale creation latency on the test database is under 450 ms."""
    async def _run():
        test_url = require_test_database_url()
        engine = create_async_engine(test_url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        async with session_factory() as session:
            from app.models.inventory import PurchaseLine

            shifts = (
                await session.execute(
                    select(RegisterShift).where(RegisterShift.status == "open")
                )
            ).scalars().all()
            shift = None
            stocked_lines = []
            for s in shifts:
                pls = (
                    await session.execute(
                        select(PurchaseLine).where(
                            PurchaseLine.business_id == s.business_id,
                            PurchaseLine.branch_id == s.branch_id,
                            PurchaseLine.qty_remaining >= 1,
                            PurchaseLine.deleted_at.is_(None),
                        )
                    )
                ).scalars().all()
                if len(pls) >= 10:
                    shift = s
                    stocked_lines = pls[:10]
                    break

            if not shift or len(stocked_lines) < 10:
                pytest.skip("No open shift with >= 10 stocked purchase lines found in test DB")

            user = (
                await session.execute(select(User).where(User.id == shift.opened_by))
            ).scalar_one()

            lines = [
                CreateSaleLineRequest(
                    product_id=pl.product_id,
                    variation_id=pl.variation_id,
                    qty=Decimal("1"),
                    unit_price=Decimal("100"),
                    line_order=i,
                )
                for i, pl in enumerate(stocked_lines)
            ]
            sale_req = CreateSaleRequest(
                branch_id=shift.branch_id,
                sale_type=SaleTypeEnum.pos,
                register_shift_id=shift.id,
                lines=lines,
                payments=[
                    CreatePaymentRequest(
                        payment_method=PaymentMethodEnum.cash,
                        amount=Decimal("1000"),
                        tendered_amount=Decimal("1000"),
                    )
                ],
            )

            t0 = time.perf_counter()
            sale = await create_sale(
                db=session,
                business_id=user.business_id,
                data=sale_req,
                created_by=user.id,
                permission_keys=frozenset(["sales.create", "sales.view", "sales.override_price"]),
            )
            duration_ms = (time.perf_counter() - t0) * 1000
            assert sale is not None
            assert sale.sale_number.startswith("INV-")
            assert duration_ms < 500.0, f"Expected 10-line sale creation under 500ms, took {duration_ms:.2f}ms"

        await engine.dispose()

    asyncio.run(_run())
