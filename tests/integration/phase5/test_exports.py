"""Exports: permissions, filters, row cap, formula injection, import round-trip."""

from __future__ import annotations

import csv
import io

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.sales import create_sale_payload
from tests.helpers.server import managed_server
from tests.helpers.tenants import build_pos_tenant

pytestmark = pytest.mark.integration

ALL_DATASETS = [
    "sales", "sale_lines", "inventory", "products", "customers", "customer_ledger",
    "suppliers", "supplier_ledger", "expenses", "purchases", "profit_loss", "tax_summary",
    "cashier_performance", "top_products",
]


def _rows(resp: httpx.Response) -> list[list[str]]:
    text = resp.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


@pytest.fixture
def tenant(http_client, api_base, unique_suffix):
    t = build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="470")
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
def test_every_dataset_exports_csv_and_xlsx(http_client, tenant, dataset):
    for fmt in ("csv", "xlsx"):
        r = http_client.get(
            tenant.url(f"/export/{dataset}"), headers=tenant.owner_headers, params={"format": fmt}
        )
        assert_status(r, 200, label=f"{dataset} {fmt}")
        assert "attachment" in r.headers["content-disposition"]
        if fmt == "csv":
            assert len(_rows(r)) >= 1  # header at least


def test_sales_csv_shape(http_client, tenant):
    r = http_client.get(tenant.url("/export/sales"), headers=tenant.owner_headers)
    rows = _rows(r)
    header = rows[0]
    assert header[:3] == ["sale_number", "client_receipt_no", "sold_at"]
    assert len(rows) == 2
    rec = dict(zip(header, rows[1]))
    assert rec["total"] == "300.00" and rec["paid"] == "150.00" and rec["balance_due"] == "150.00"
    assert "Rs" not in r.text and "PKR" not in r.text
    assert len(rec["sold_at"]) == 16  # YYYY-MM-DD HH:MM


def test_sale_lines_csv(http_client, tenant):
    rows = _rows(http_client.get(tenant.url("/export/sale_lines"), headers=tenant.owner_headers))
    rec = dict(zip(rows[0], rows[1]))
    assert rec["qty"] == "2" and rec["unit_price"] == "150.00" and rec["line_total"] == "300.00"


def test_date_filter_excludes_sales(http_client, tenant):
    r = http_client.get(
        tenant.url("/export/sales"), headers=tenant.owner_headers,
        params={"date_from": "2020-01-01", "date_to": "2020-01-31"},
    )
    assert len(_rows(r)) == 1


def test_bad_date_range_and_format_and_dataset(http_client, tenant):
    h = tenant.owner_headers
    r = http_client.get(tenant.url("/export/sales"), headers=h,
                        params={"date_from": "2024-02-01", "date_to": "2024-01-01"})
    assert r.status_code == 422
    assert http_client.get(tenant.url("/export/sales"), headers=h, params={"format": "pdf"}).status_code == 422
    assert http_client.get(tenant.url("/export/nope"), headers=h).status_code == 404


def test_branch_filter(http_client, tenant):
    other = http_client.post(
        tenant.url("/branches"), headers=tenant.owner_headers, json={"name": "Second"}
    )
    assert_status(other, 201, label="branch")
    r = http_client.get(
        tenant.url("/export/sales"), headers=tenant.owner_headers,
        params={"branch_id": other.json()["id"]},
    )
    assert len(_rows(r)) == 1
    r = http_client.get(
        tenant.url("/export/sales"), headers=tenant.owner_headers,
        params={"branch_id": tenant.branch_id},
    )
    assert len(_rows(r)) == 2


def test_cashier_cannot_export(http_client, tenant):
    for path in ("/export/sales", "/export"):
        r = http_client.get(tenant.url(path), headers=tenant.cashier_headers)
        assert r.status_code == 403


def test_listing_shows_columns(http_client, tenant):
    body = http_client.get(tenant.url("/export"), headers=tenant.owner_headers).json()
    names = {d["name"] for d in body["datasets"]}
    assert set(ALL_DATASETS) <= names
    assert body["max_rows"] > 0


def test_formula_injection_csv_and_xlsx(http_client, tenant):
    name = "=HYPERLINK(\"http://evil\",\"x\")"
    r = http_client.post(
        tenant.url("/products"), headers=tenant.owner_headers,
        json={"name": name, "base_unit_id": tenant.unit_id, "product_type": "standard",
              "tracking_type": "none"},
    )
    assert_status(r, 201, label="evil product")
    csv_resp = http_client.get(tenant.url("/export/products"), headers=tenant.owner_headers)
    names = [row[0] for row in _rows(csv_resp)[1:]]
    assert "'" + name in names and name not in names

    from openpyxl import load_workbook

    x = http_client.get(tenant.url("/export/products"), headers=tenant.owner_headers,
                        params={"format": "xlsx"})
    ws = load_workbook(io.BytesIO(x.content)).active
    for row in ws.iter_rows():
        for cell in row:
            assert cell.data_type != "f", f"formula cell {cell.coordinate}"
    assert any(c.value == "'" + name for row in ws.iter_rows() for c in row)


def test_products_export_is_importable(http_client, tenant):
    r = http_client.post(
        tenant.url("/products"), headers=tenant.owner_headers,
        json={"name": "Round Trip", "sku": "RT-1", "base_unit_id": tenant.unit_id,
              "product_type": "standard", "tracking_type": "none"},
    )
    assert_status(r, 201, label="sku product")
    exported = http_client.get(tenant.url("/export/products"), headers=tenant.owner_headers)
    rows = _rows(exported)
    assert rows[0] == ["name", "sku", "barcode", "category", "unit", "cost", "price", "stock"]
    keep = [row for row in rows if row[1] == "RT-1"]
    assert keep
    buf = io.StringIO()
    csv.writer(buf).writerows([rows[0], *keep])
    r = http_client.post(
        tenant.url("/products/import"), headers=tenant.owner_headers,
        json={"csv": buf.getvalue(), "dry_run": True},
    )
    assert_status(r, 200, label="dry run")
    summary = r.json()["summary"]
    assert summary["errors"] == 0 and summary["total_rows"] == 1


def test_unit_helpers_sanitize():
    from app.services.export_datasets import sanitize_cell

    assert sanitize_cell("=1+1") == "'=1+1"
    assert sanitize_cell("@SUM(A1)") == "'@SUM(A1)"
    assert sanitize_cell("-cmd") == "'-cmd"
    assert sanitize_cell("-5.00") == "-5.00"
    assert sanitize_cell("+923001234567") == "+923001234567"
    assert sanitize_cell("\tx") == "'\tx"
    assert sanitize_cell("plain") == "plain"


def test_row_cap_returns_413(unique_suffix):
    with managed_server({"EXPORT_MAX_ROWS": "2"}) as base:
        with httpx.Client(timeout=60) as c:
            t = build_pos_tenant(c, base, unique_suffix, phone_prefix="471")
            for _ in range(3):
                r = c.post(t.url("/sales"), headers=t.owner_headers,
                           json=create_sale_payload(branch_id=t.branch_id, product_id=t.product_id,
                                                    register_shift_id=t.shift_id))
                assert_status(r, 201, label="sale")
            r = c.get(t.url("/export/sales"), headers=t.owner_headers)
            assert r.status_code == 413
            assert "Export too large" in r.json()["detail"]
            assert c.get(t.url("/export/products"), headers=t.owner_headers).status_code == 200
