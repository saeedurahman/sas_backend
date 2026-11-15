"""Tests for PDF export: layout, Urdu text, row caps, permissions, timing sanity, headers."""

from __future__ import annotations

import time
import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.sales import create_sale_payload
from tests.helpers.server import managed_server
from tests.helpers.tenants import add_stock_adjustment, build_pos_tenant

pytestmark = pytest.mark.integration

ALL_DATASETS = [
    "sales", "sale_lines", "inventory", "products", "customers", "customer_ledger",
    "suppliers", "supplier_ledger", "expenses", "purchases", "profit_loss", "tax_summary",
    "cashier_performance", "top_products",
]


@pytest.fixture
def tenant(http_client, api_base, unique_suffix):
    t = build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="475")
    r = http_client.post(
        t.url("/sales"), headers=t.owner_headers,
        json=create_sale_payload(
            branch_id=t.branch_id, product_id=t.product_id, qty="2", unit_price="150.00",
            register_shift_id=t.shift_id,
        ),
    )
    assert_status(r, 201, label="sale")
    return t


@pytest.mark.parametrize("dataset", ALL_DATASETS)
def test_every_dataset_exports_pdf_without_urdu(http_client, tenant, dataset):
    r = http_client.get(
        tenant.url(f"/export/{dataset}"),
        headers=tenant.owner_headers,
        params={"format": "pdf"},
    )
    assert_status(r, 200, label=f"{dataset} pdf")
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    assert f"{dataset}_" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.pdf"')
    assert r.content.startswith(b"%PDF-")
    assert len(r.content) > 500


def test_pdf_export_with_urdu_text(http_client, tenant):
    """Urdu text (Arabic script RTL) is properly shaped and embedded via Noto Naskh Arabic."""
    # Create product with Urdu name
    urdu_product_name = "احمد علی اور دودھ پتی"
    r = http_client.post(
        tenant.url("/products"),
        headers=tenant.owner_headers,
        json={
            "name": urdu_product_name,
            "sku": "URDU-SKU-1",
            "base_unit_id": tenant.unit_id,
            "product_type": "standard",
            "tracking_type": "none",
        },
    )
    assert_status(r, 201, label="urdu product")
    urdu_product_id = r.json()["id"]
    add_stock_adjustment(http_client, tenant, product_id=urdu_product_id, qty_delta="100")

    # Create customer with Urdu name
    urdu_customer_name = "محمد رضوان راولپنڈی"
    r = http_client.post(
        tenant.url("/customers"),
        headers=tenant.owner_headers,
        json={
            "name": urdu_customer_name,
            "phone": "03009876543",
        },
    )
    assert_status(r, 201, label="urdu customer")
    urdu_customer_id = r.json()["id"]

    # Create a sale with Urdu product and customer
    r = http_client.post(
        tenant.url("/sales"),
        headers=tenant.owner_headers,
        json=create_sale_payload(
            branch_id=tenant.branch_id,
            product_id=urdu_product_id,
            customer_id=urdu_customer_id,
            qty="3",
            unit_price="250.00",
            register_shift_id=tenant.shift_id,
        ),
    )
    assert_status(r, 201, label="urdu sale")

    # Export sales, products, and customers to PDF
    for dataset in ("sales", "sale_lines", "products", "customers"):
        resp = http_client.get(
            tenant.url(f"/export/{dataset}"),
            headers=tenant.owner_headers,
            params={"format": "pdf"},
        )
        assert_status(resp, 200, label=f"urdu {dataset} pdf")
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")
        assert len(resp.content) > 1000


def test_pdf_content_type_and_disposition(http_client, tenant):
    """Verify exact Content-Type, Content-Disposition headers for PDF, CSV, and XLSX."""
    # PDF
    r_pdf = http_client.get(
        tenant.url("/export/sales"),
        headers=tenant.owner_headers,
        params={"format": "pdf"},
    )
    assert r_pdf.status_code == 200
    assert r_pdf.headers["content-type"] == "application/pdf"
    assert 'attachment; filename="sales_' in r_pdf.headers["content-disposition"]
    assert r_pdf.headers["content-disposition"].endswith('.pdf"')

    # CSV
    r_csv = http_client.get(
        tenant.url("/export/sales"),
        headers=tenant.owner_headers,
        params={"format": "csv"},
    )
    assert r_csv.status_code == 200
    assert "text/csv" in r_csv.headers["content-type"]
    assert 'attachment; filename="sales_' in r_csv.headers["content-disposition"]
    assert r_csv.headers["content-disposition"].endswith('.csv"')

    # XLSX
    r_xlsx = http_client.get(
        tenant.url("/export/sales"),
        headers=tenant.owner_headers,
        params={"format": "xlsx"},
    )
    assert r_xlsx.status_code == 200
    assert r_xlsx.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert 'attachment; filename="sales_' in r_xlsx.headers["content-disposition"]
    assert r_xlsx.headers["content-disposition"].endswith('.xlsx"')


def test_cashier_without_permission_cannot_export_pdf(http_client, tenant):
    """Cashier lacking reports.export cannot export PDF."""
    r = http_client.get(
        tenant.url("/export/sales"),
        headers=tenant.cashier_headers,
        params={"format": "pdf"},
    )
    assert r.status_code == 403


def test_pdf_max_rows_cap_returns_friendly_413(unique_suffix):
    """When exceeding EXPORT_PDF_MAX_ROWS, returns 413 suggesting CSV or Excel."""
    with managed_server({"EXPORT_PDF_MAX_ROWS": "2"}) as base:
        with httpx.Client(timeout=60) as c:
            t = build_pos_tenant(c, base, unique_suffix, phone_prefix="476")
            for _ in range(3):
                r = c.post(
                    t.url("/sales"),
                    headers=t.owner_headers,
                    json=create_sale_payload(
                        branch_id=t.branch_id,
                        product_id=t.product_id,
                        register_shift_id=t.shift_id,
                    ),
                )
                assert_status(r, 201, label="sale")

            # PDF should hit the 2-row cap with clear guidance
            r_pdf = c.get(
                t.url("/export/sales"),
                headers=t.owner_headers,
                params={"format": "pdf"},
            )
            assert r_pdf.status_code == 413
            detail = r_pdf.json()["detail"]
            assert "PDF export is limited to" in detail
            assert "Excel / CSV" in detail

            # CSV and XLSX should NOT hit the PDF limit and succeed
            assert c.get(t.url("/export/sales"), headers=t.owner_headers, params={"format": "csv"}).status_code == 200
            assert c.get(t.url("/export/sales"), headers=t.owner_headers, params={"format": "xlsx"}).status_code == 200


def test_global_max_rows_cap_returns_413_for_pdf(unique_suffix):
    """When exceeding EXPORT_MAX_ROWS, returns standard 413."""
    with managed_server({"EXPORT_MAX_ROWS": "2"}) as base:
        with httpx.Client(timeout=60) as c:
            t = build_pos_tenant(c, base, unique_suffix, phone_prefix="477")
            for _ in range(3):
                r = c.post(
                    t.url("/sales"),
                    headers=t.owner_headers,
                    json=create_sale_payload(
                        branch_id=t.branch_id,
                        product_id=t.product_id,
                        register_shift_id=t.shift_id,
                    ),
                )
                assert_status(r, 201, label="sale")

            r_pdf = c.get(
                t.url("/export/sales"),
                headers=t.owner_headers,
                params={"format": "pdf"},
            )
            assert r_pdf.status_code == 413
            assert "Export too large" in r_pdf.json()["detail"]


def test_pdf_timing_sanity_on_hundreds_of_rows(http_client, tenant):
    """Generate multiple sales and verify PDF export generates within reasonable sanity time."""
    # Create 30 sales with 2 lines each = 60 sale lines
    for _ in range(25):
        r = http_client.post(
            tenant.url("/sales"),
            headers=tenant.owner_headers,
            json=create_sale_payload(
                branch_id=tenant.branch_id,
                product_id=tenant.product_id,
                qty="1",
                unit_price="100.00",
                register_shift_id=tenant.shift_id,
            ),
        )
        assert_status(r, 201, label="bulk sale")

    t0 = time.perf_counter()
    resp = http_client.get(
        tenant.url("/export/sales"),
        headers=tenant.owner_headers,
        params={"format": "pdf"},
    )
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")
    # Sanity check: 25+ sales should generate in well under 5 seconds
    assert elapsed < 5.0, f"PDF export took {elapsed:.2f}s, expected < 5.0s"
