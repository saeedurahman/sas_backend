"""GET /products/lookup."""

from __future__ import annotations

import time

import pytest

from tests.helpers.phase2 import D, second_branch, stock_in
from tests.integration.quick_add.helpers import ean13, line, lookup, quick_add

pytestmark = pytest.mark.integration


def test_not_found_has_null_suggestion(http_client, pos_tenant):
    code = ean13()
    r = lookup(http_client, pos_tenant, code)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["found"] is False
    assert b["product"] is None
    assert "suggestion" in b and b["suggestion"] is None
    assert b["barcode"]["normalized"] == code and b["barcode"]["valid"] is True
    assert b["barcode"]["format"] == "EAN13"


def test_found_returns_price_cost_stock_and_unit(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    res = quick_add(
        http_client, t,
        [line(code, name="Lookup Me", qty="12", cost_price="40", sale_price="55.50", track_expiry=True, expiry_date="2031-01-31")],
    )
    pid = res["lines"][0]["product_id"]
    b = lookup(http_client, t, code).json()
    assert b["found"] is True
    p = b["product"]
    assert p["id"] == pid and p["name"] == "Lookup Me"
    assert p["variation_id"] == res["lines"][0]["variation_id"]
    assert D(p["sale_price"]) == D("55.50")
    assert D(p["cost_price"]) == D("40")
    assert D(p["stock_on_hand"]) == D("12")
    assert p["track_expiry"] is True
    assert p["unit"]["id"] == t.unit_id
    assert b["barcode"]["matched_barcode"] == code


def test_lookup_matches_across_forms(http_client, pos_tenant):
    t = pos_tenant
    code = ean13("036")  # EAN-13 starting with 0 == a UPC-A (drop the leading 0)
    quick_add(http_client, t, [line(code, qty="0")])
    upc = code[1:]
    assert lookup(http_client, t, upc).json()["found"] is True
    assert lookup(http_client, t, " " + code + " ").json()["found"] is True
    assert lookup(http_client, t, "0" + code).json()["found"] is True  # GTIN-14 padded


def test_lookup_without_price_or_cost(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    quick_add(http_client, t, [line(code, qty="0", cost_price="0", sale_price="0")])
    p = lookup(http_client, t, code).json()["product"]
    assert p["sale_price"] is None and p["cost_price"] is None
    assert D(p["stock_on_hand"]) == 0


def test_stock_on_hand_is_per_branch(http_client, pos_tenant):
    t = pos_tenant
    other = second_branch(http_client, t)
    code = ean13()
    res = quick_add(http_client, t, [line(code, qty="5")])
    pid = res["lines"][0]["product_id"]
    stock_in(http_client, t, pid, "7", branch_id=other)
    here = lookup(http_client, t, code, branch_id=t.branch_id).json()["product"]
    there = lookup(http_client, t, code, branch_id=other).json()["product"]
    both = lookup(http_client, t, code).json()["product"]
    assert D(here["stock_on_hand"]) == 5
    assert D(there["stock_on_hand"]) == 7
    assert D(both["stock_on_hand"]) == 12


def test_other_business_products_are_never_returned(http_client, pos_tenant, rbac_tenant):
    code = ean13()
    quick_add(http_client, pos_tenant, [line(code)])
    assert lookup(http_client, rbac_tenant, code).json()["found"] is False


def test_lookup_needs_products_view_and_a_barcode(http_client, pos_tenant):
    t = pos_tenant
    assert http_client.get(t.url("/products/lookup"), headers=t.owner_headers).status_code == 422
    assert http_client.get(t.url("/products/lookup"), params={"barcode": "1"}).status_code in (401, 403)
    # a cashier can scan
    assert lookup(http_client, t, ean13(), headers=t.cashier_headers).status_code == 200


def test_lookup_is_fast(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    quick_add(http_client, t, [line(code)])
    lookup(http_client, t, code)  # warm up
    samples = []
    for _ in range(30):
        s = time.perf_counter()
        assert lookup(http_client, t, code).status_code == 200
        samples.append(time.perf_counter() - s)
    samples.sort()
    # loose bound (HTTP round trip on a shared CI box); the query itself is one indexed read
    assert samples[int(len(samples) * 0.95) - 1] < 0.25, samples
