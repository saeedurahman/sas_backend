"""Product CSV import and image upload."""

from __future__ import annotations

import base64
from decimal import Decimal as D

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.sqlexec import sql
from tests.helpers.tenants import build_pos_tenant

pytestmark = pytest.mark.integration

HEADER = "name,sku,barcode,category,unit,cost,price,opening_stock\n"
# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def t(http_client, api_base, unique_suffix):
    return build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="476")


def _import(c, t, csv_text, *, dry_run, headers=None, **extra):
    return c.post(t.url("/products/import"), headers=headers or t.owner_headers,
                  json={"csv": csv_text, "dry_run": dry_run, **extra})


def test_dry_run_reports_per_row_and_writes_nothing(http_client, t):
    c = http_client
    body = (HEADER
            + "Soap,SOAP-1,8901234567890,Toiletries,Piece,40,60,25\n"
            + "Bad Unit,BU-1,,Misc,Parsec,1,2,\n"
            + ",NO-NAME,,,Piece,1,2,\n"
            + "Neg,NEG-1,,,Piece,-1,2,\n")
    r = _import(c, t, body, dry_run=True)
    assert_status(r, 200, label="dry run")
    rep = r.json()
    assert rep["dry_run"] is True
    s = rep["summary"]
    assert s["total_rows"] == 4 and s["create"] == 1 and s["errors"] == 3 and s["applied"] == 0
    rows = {row["row"]: row for row in rep["rows"]}
    assert rows[2]["action"] == "create"
    assert all(rows[n]["action"] == "error" and rows[n]["errors"] for n in (3, 4, 5))
    assert sql("SELECT count(*) FROM products WHERE business_id=:b AND sku = 'SOAP-1'", b=t.business_id)[0][0] == 0


def test_apply_is_atomic_in_abort_mode(http_client, t):
    body = HEADER + "Good,G-1,,,Piece,1,2,\nBad,B-1,,,Parsec,1,2,\n"
    r = _import(http_client, t, body, dry_run=False)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "import_has_errors"
    assert sql("SELECT count(*) FROM products WHERE business_id=:b AND sku = 'G-1'", b=t.business_id)[0][0] == 0


def test_apply_creates_price_barcode_category_and_opening_stock(http_client, t):
    c = http_client
    body = HEADER + "Soap,SOAP-1,8901234567890,Toiletries,Piece,40,60,25\n"
    r = _import(c, t, body, dry_run=False)
    assert_status(r, 200, label="apply")
    assert r.json()["summary"]["applied"] == 1

    pid = sql("SELECT id FROM products WHERE business_id=:b AND sku='SOAP-1'", b=t.business_id)[0][0]
    assert sql("SELECT count(*) FROM categories WHERE business_id=:b AND name='Toiletries'",
               b=t.business_id)[0][0] == 1
    price = c.get(t.url(f"/prices/product/{pid}"), headers=t.owner_headers).json()["unit_price"]
    assert D(price) == D("60")
    qty = sql("SELECT COALESCE(SUM(qty),0) FROM stock_levels WHERE product_id=:p AND deleted_at IS NULL", p=pid)[0][0]
    assert D(str(qty)) == D("25")
    assert sql("SELECT count(*) FROM barcodes WHERE product_id=:p AND barcode='8901234567890'", p=pid)[0][0] == 1


def test_rerun_is_idempotent_and_updates_by_sku_or_barcode(http_client, t):
    c = http_client
    body = HEADER + "Soap,SOAP-1,8901234567890,Toiletries,Piece,40,60,25\n"
    assert _import(c, t, body, dry_run=False).status_code == 200
    again = _import(c, t, body, dry_run=False).json()
    assert again["summary"]["create"] == 0 and again["summary"]["unchanged"] == 1
    assert sql("SELECT count(*) FROM products WHERE business_id=:b AND sku='SOAP-1'", b=t.business_id)[0][0] == 1
    # opening stock is not applied twice
    pid = sql("SELECT id FROM products WHERE business_id=:b AND sku='SOAP-1'", b=t.business_id)[0][0]
    qty = sql("SELECT COALESCE(SUM(qty),0) FROM stock_levels WHERE product_id=:p AND deleted_at IS NULL", p=pid)[0][0]
    assert D(str(qty)) == D("25")

    # same barcode, new sku/name/price -> update, not a duplicate
    changed = _import(c, t, HEADER + "Soap Deluxe,,8901234567890,Toiletries,Piece,40,75,\n", dry_run=False).json()
    assert changed["summary"]["update"] == 1
    assert D(c.get(t.url(f"/prices/product/{pid}"), headers=t.owner_headers).json()["unit_price"]) == D("75")


def test_in_file_duplicates_and_row_cap(http_client, t):
    body = HEADER + "A,DUP,,,Piece,1,2,\nB,DUP,,,Piece,1,2,\n"
    rep = _import(http_client, t, body, dry_run=True).json()
    assert rep["summary"]["errors"] >= 1
    big = HEADER + "".join(f"P{i},S{i},,,Piece,1,2,\n" for i in range(5001))
    assert _import(http_client, t, big, dry_run=True).status_code == 413


def test_import_requires_permission(http_client, t):
    r = _import(http_client, t, HEADER + "X,X-1,,,Piece,1,2,\n", dry_run=True, headers=t.cashier_headers)
    assert r.status_code == 403


def test_rows_json_alternative_and_bad_request(http_client, t):
    r = http_client.post(t.url("/products/import"), headers=t.owner_headers,
                         json={"rows": [{"name": "Json Item", "sku": "J-1", "unit": "Piece", "price": "5"}],
                               "dry_run": True})
    assert_status(r, 200, label="rows json")
    assert r.json()["summary"]["create"] == 1, r.json()
    # neither csv nor rows -> 422
    assert http_client.post(t.url("/products/import"), headers=t.owner_headers,
                            json={"dry_run": True}).status_code == 422


# ── uploads ──────────────────────────────────────────────────────────────────


def _upload(c, t, content, name="p.png", ctype="image/png", headers=None):
    return c.post(t.url("/uploads/image"), headers=headers or t.owner_headers,
                  files={"file": (name, content, ctype)})


def test_upload_local_image_is_served(http_client, t):
    r = _upload(http_client, t, PNG)
    assert_status(r, 201, label="upload")
    url = r.json()["url"]
    assert set(r.json()) == {"url"}
    served = http_client.get(url if url.startswith("http") else t.url("/")[: -len("/api/v1/")] + url)
    assert served.status_code == 200 and served.content == PNG


def test_upload_rejects_wrong_type_and_spoofed_content(http_client, t):
    assert _upload(http_client, t, b"<script>alert(1)</script>", "x.png", "image/png").status_code == 415
    # the stored extension comes from the sniffed bytes, never from the client's file name
    r = _upload(http_client, t, PNG, "x.svg", "image/svg+xml")
    assert r.status_code == 201 and r.json()["url"].endswith(".png")
    assert _upload(http_client, t, b"<svg xmlns='http://www.w3.org/2000/svg'/>", "x.svg", "image/svg+xml").status_code == 415
    assert _upload(http_client, t, b"%PDF-1.4", "x.pdf", "application/pdf").status_code == 415


def test_upload_permission(http_client, t):
    assert _upload(http_client, t, PNG, headers=t.cashier_headers).status_code == 403
    assert http_client.post(t.url("/uploads/image"), files={"file": ("p.png", PNG, "image/png")}).status_code in (401, 403)


def test_upload_size_limit(unique_suffix):
    from tests.helpers.server import managed_server

    with managed_server({"UPLOAD_MAX_IMAGE_BYTES": "1024"}) as base:
        with httpx.Client(timeout=60) as c:
            t = build_pos_tenant(c, base, unique_suffix, phone_prefix="477")
            big = PNG + b"\0" * 4096
            r = _upload(c, t, big)
            assert r.status_code == 413
            assert _upload(c, t, PNG).status_code == 201
