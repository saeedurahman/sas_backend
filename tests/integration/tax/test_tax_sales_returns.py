"""Tax / price settings on real sales: both modes, rounding, credit, returns, void, receipts."""

from __future__ import annotations

import pytest

from tests.helpers.db import db_execute, db_rows, db_scalar
from tests.helpers.phase2 import D, create_customer, customer_balance, post_sale
from tests.integration.tax.helpers import configure, make_rate, put_setting, sale, use_rate_on_product

pytestmark = pytest.mark.integration


@pytest.fixture
def t17(http_client, pos_tenant):
    """POS tenant whose product carries a 17% rate."""
    use_rate_on_product(pos_tenant, make_rate(http_client, pos_tenant, "17"))
    return pos_tenant


def _sale_row(sale_id: str) -> dict:
    return db_rows("SELECT * FROM sales WHERE id = :i", {"i": sale_id})[0]


# ── sale in each mode ────────────────────────────────────────────────────────


@pytest.mark.parametrize("qty, total", [("1", "7.02"), ("2", "14.04"), ("3", "21.06")])
def test_exclusive_sale(http_client, t17, qty, total):
    r = sale(http_client, t17, qty=qty, price="6", pay=total)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["tax_inclusive"] is False and b["tax_rounding_mode"] == "none"
    assert D(b["total_amount"]) == D(total) and D(b["rounding_amount"]) == 0
    assert D(b["net_amount"]) + D(b["total_tax"]) == D(total)
    line = b["lines"][0]
    assert D(line["net_amount"]) + D(line["tax_amount"]) == D(total)


def test_inclusive_sale(http_client, t17):
    configure(http_client, t17, {"tax.prices_include_tax": True})
    r = sale(http_client, t17, qty="1", price="6", pay="6.00")
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["tax_inclusive"] is True
    assert (D(b["total_amount"]), D(b["total_tax"]), D(b["net_amount"])) == (D("6.00"), D("0.87"), D("5.13"))
    row = _sale_row(b["id"])
    assert row["tax_inclusive"] is True and D(row["rounding_amount"]) == 0
    line = db_rows("SELECT * FROM sale_lines WHERE sale_id = :i", {"i": b["id"]})[0]
    assert (D(line["net_amount"]), D(line["tax_amount"])) == (D("5.13"), D("0.87"))


def test_price_preview_matches_sale(http_client, t17):
    configure(http_client, t17, {"tax.prices_include_tax": True, "tax.rounding_mode": "nearest_5"})
    body = {"branch_id": t17.branch_id, "lines": [{"product_id": t17.product_id, "qty": "3", "unit_price": "6"}]}
    r = http_client.post(t17.url("/sales/price-preview"), headers=t17.owner_headers, json=body)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["tax_inclusive"] is True and p["tax_rounding_mode"] == "nearest_5"
    assert (D(p["exact_total"]), D(p["rounding_amount"]), D(p["total_amount"])) == (D("18.00"), D("2.00"), D("20.00"))
    assert D(p["net_amount"]) + D(p["total_tax"]) == D("18.00")
    s = sale(http_client, t17, qty="3", price="6", pay="20")
    assert s.status_code == 201, s.text
    assert D(s.json()["total_amount"]) == D(p["total_amount"])


def test_cart_level_discount(http_client, t17):
    configure(http_client, t17, {"tax.prices_include_tax": True})
    r = sale(http_client, t17, qty="10", price="6", pay="55", cart_discount_amount="5")
    assert r.status_code == 201, r.text
    b = r.json()
    assert D(b["total_amount"]) == D("55.00") and D(b["total_discount"]) == D("5.00")


# ── rounding + payments ──────────────────────────────────────────────────────


def test_rounding_applies_to_grand_total_only(http_client, t17):
    configure(http_client, t17, {"tax.rounding_mode": "nearest_5"})
    r = sale(http_client, t17, qty="3", price="6", pay="20")
    assert r.status_code == 201, r.text
    b = r.json()
    assert D(b["exact_total"]) == D("21.06")
    assert D(b["rounding_amount"]) == D("-1.06") and D(b["total_amount"]) == D("20.00")
    # lines keep cents
    assert D(b["lines"][0]["tax_amount"]) == D("3.06")
    row = _sale_row(b["id"])
    assert row["tax_rounding_mode"] == "nearest_5" and D(row["rounding_amount"]) == D("-1.06")


def test_split_payments_must_match_rounded_total(http_client, t17):
    configure(http_client, t17, {"tax.rounding_mode": "nearest_5"})
    ok = post_sale(
        http_client, t17, qty="3", unit_price="6",
        payments=[{"payment_method": "cash", "amount": "10"}, {"payment_method": "card", "amount": "10"}],
    )
    assert ok.status_code == 201, ok.text
    assert D(ok.json()["total_amount"]) == D("20.00")
    short = post_sale(
        http_client, t17, qty="3", unit_price="6",
        payments=[{"payment_method": "cash", "amount": "10"}, {"payment_method": "card", "amount": "9"}],
    )
    # under the rounded total (20.00) the sale is not complete
    assert short.status_code == 201 and short.json()["status"] == "partially_paid"


def test_credit_sale_with_rounding_and_void(http_client, t17):
    configure(http_client, t17, {"tax.rounding_mode": "nearest_5"})
    cust = create_customer(http_client, t17)
    r = post_sale(
        http_client, t17, qty="3", unit_price="6", customer_id=cust,
        payments=[{"payment_method": "cash", "amount": "5"}, {"payment_method": "credit", "amount": "15"}],
    )
    assert r.status_code == 201, r.text
    assert customer_balance(http_client, t17, cust) == D("-15")
    sid = r.json()["id"]
    v = http_client.post(t17.url(f"/sales/{sid}/void"), headers=t17.owner_headers)
    assert v.status_code == 200, v.text
    assert customer_balance(http_client, t17, cust) == D("0")
    # void reverses what was recorded: the stored figures are untouched
    row = _sale_row(sid)
    assert row["status"] == "voided" and D(row["rounding_amount"]) == D("-1.06")
    line = db_rows("SELECT * FROM sale_lines WHERE sale_id = :i", {"i": sid})[0]
    assert D(line["tax_amount"]) == D("3.06")


# ── settings changes apply only to new sales ─────────────────────────────────


def test_settings_change_applies_only_to_new_sales(http_client, t17):
    old = sale(http_client, t17, qty="1", price="6", pay="7.02")
    assert old.status_code == 201
    configure(http_client, t17, {"tax.prices_include_tax": True, "tax.rounding_mode": "nearest_10"})
    new = sale(http_client, t17, qty="1", price="6", pay="10")
    assert new.status_code == 201, new.text
    assert new.json()["tax_inclusive"] is True and D(new.json()["total_amount"]) == D("10.00")
    again = _sale_row(old.json()["id"])
    assert again["tax_inclusive"] is False and again["tax_rounding_mode"] == "none"
    got = http_client.get(t17.url(f"/sales/{old.json()['id']}"), headers=t17.owner_headers).json()
    assert D(got["total_amount"]) == D("7.02") and got["tax_inclusive"] is False


# ── tax.enabled / default rate / exempt ──────────────────────────────────────


@pytest.mark.parametrize("inclusive", [False, True])
def test_tax_disabled_gives_identical_totals(http_client, t17, inclusive):
    configure(http_client, t17, {"tax.enabled": False, "tax.prices_include_tax": inclusive})
    r = sale(http_client, t17, qty="3", price="6", pay="18")
    assert r.status_code == 201, r.text
    b = r.json()
    assert D(b["total_tax"]) == 0 and D(b["total_amount"]) == D("18.00") and D(b["net_amount"]) == D("18.00")


def test_default_rate_used_when_product_has_none(http_client, pos_tenant):
    t = pos_tenant
    configure(http_client, t, {"tax.default_rate_id": make_rate(http_client, t, "10", "Std")})
    r = sale(http_client, t, qty="1", price="100", pay="110")
    assert r.status_code == 201, r.text
    assert D(r.json()["lines"][0]["tax_rate"]) == D("10") and D(r.json()["total_tax"]) == D("10.00")


def test_exempt_product_has_no_tax(http_client, pos_tenant):
    t = pos_tenant
    configure(http_client, t, {"tax.default_rate_id": make_rate(http_client, t, "10", "Std")})
    db_execute("UPDATE products SET is_tax_exempt = TRUE WHERE id = :p", {"p": t.product_id})
    r = sale(http_client, t, qty="1", price="100", pay="100")
    assert r.status_code == 201, r.text
    assert D(r.json()["total_tax"]) == 0


# ── returns ──────────────────────────────────────────────────────────────────


def _return(client, t, sale_json, qty: str, refund: str, line_idx: int = 0):
    body = {
        "branch_id": t.branch_id,
        "sale_id": sale_json["id"],
        "lines": [{"sale_line_id": sale_json["lines"][line_idx]["id"], "qty": qty}],
        "refund_payments": [{"payment_method": "cash", "amount": refund}],
    }
    return client.post(t.url("/returns"), headers=t.owner_headers, json=body)


@pytest.mark.parametrize("inclusive", [False, True])
@pytest.mark.parametrize("mode", ["none", "nearest_5", "nearest_10"])
def test_full_return_refunds_exactly_the_rounded_total(http_client, t17, inclusive, mode):
    configure(http_client, t17, {"tax.prices_include_tax": inclusive, "tax.rounding_mode": mode})
    s = sale(http_client, t17, qty="3", price="6", pay="25")  # overpay: change is fine
    assert s.status_code == 201, s.text
    sj = s.json()
    # switching settings afterwards must not matter: the sale snapshot rules
    configure(http_client, t17, {"tax.prices_include_tax": not inclusive, "tax.rounding_mode": "nearest_1"})
    r = _return(http_client, t17, sj, "3", sj["total_amount"])
    assert r.status_code == 201, r.text
    rj = r.json()
    assert D(rj["total_refund"]) == D(sj["total_amount"])
    assert rj["tax_inclusive"] is inclusive and rj["tax_rounding_mode"] == mode


def test_partial_returns_add_up_to_original(http_client, t17):
    configure(http_client, t17, {"tax.rounding_mode": "nearest_5"})
    sj = sale(http_client, t17, qty="3", price="6", pay="20").json()
    first = _return(http_client, t17, sj, "1", "5.00")
    assert first.status_code == 201, first.text
    f = first.json()
    assert D(f["lines"][0]["net_amount"]) == D("6.00") and D(f["lines"][0]["tax_amount"]) == D("1.02")
    assert D(f["rounding_amount"]) == D("-2.02") and D(f["total_refund"]) == D("5.00")
    second = _return(http_client, t17, sj, "2", "15.00")
    assert second.status_code == 201, second.text
    s2 = second.json()
    assert D(f["total_refund"]) + D(s2["total_refund"]) == D(sj["total_amount"]) == D("20.00")


def test_partial_return_prorates_inclusive_lines(http_client, t17):
    configure(http_client, t17, {"tax.prices_include_tax": True})
    sj = sale(http_client, t17, qty="3", price="6", pay="18").json()
    parts = [_return(http_client, t17, sj, "1", "6.00").json() for _ in range(3)]
    assert sum(D(p["total_refund"]) for p in parts) == D("18.00")
    assert sum(D(p["lines"][0]["tax_amount"]) for p in parts) == D(sj["total_tax"])
    assert sum(D(p["lines"][0]["net_amount"]) for p in parts) == D(sj["net_amount"])


# ── receipt / invoice ────────────────────────────────────────────────────────


def test_invoice_exposes_tax_breakdown(http_client, t17):
    configure(http_client, t17, {"tax.prices_include_tax": True, "tax.rounding_mode": "nearest_5", "tax.label": "VAT"})
    sj = sale(http_client, t17, qty="3", price="6", pay="20").json()
    r = http_client.get(t17.url(f"/invoice/{sj['id']}"), headers=t17.owner_headers)
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    assert s["tax_inclusive"] is True and s["tax_label"] == "VAT" and s["show_tax_breakdown"] is True
    assert (D(s["net_amount"]), D(s["total_tax"]), D(s["exact_total"])) == (D("15.38"), D("2.62"), D("18.00"))
    assert (D(s["rounding_amount"]), D(s["total_amount"])) == (D("2.00"), D("20.00"))
    assert s["tax_breakdown"] == [
        {"tax_label": "VAT", "tax_rate": "17.00", "net_amount": "15.38", "tax_amount": "2.62"}
    ] or [(i["tax_label"], D(i["tax_rate"]), D(i["tax_amount"])) for i in s["tax_breakdown"]] == [
        ("VAT", D("17"), D("2.62"))
    ]


def test_breakdown_hidden_when_setting_off_or_no_tax(http_client, t17):
    configure(http_client, t17, {"tax.show_breakdown_on_receipt": False})
    sj = sale(http_client, t17, qty="1", price="6", pay="7.02").json()
    s = http_client.get(t17.url(f"/invoice/{sj['id']}"), headers=t17.owner_headers).json()["summary"]
    assert s["show_tax_breakdown"] is False
    configure(http_client, t17, {"tax.show_breakdown_on_receipt": True, "tax.enabled": False})
    sj = sale(http_client, t17, qty="1", price="6", pay="6").json()
    s = http_client.get(t17.url(f"/invoice/{sj['id']}"), headers=t17.owner_headers).json()["summary"]
    assert s["show_tax_breakdown"] is False and s["tax_breakdown"] == []


def test_public_receipt_shows_tax_lines(http_client, t17):
    configure(http_client, t17, {"tax.prices_include_tax": True, "tax.rounding_mode": "nearest_5", "tax.label": "VAT"})
    sj = sale(http_client, t17, qty="3", price="6", pay="20").json()
    link = http_client.post(t17.url(f"/sales/{sj['id']}/receipt-link"), headers=t17.owner_headers).json()
    html = http_client.get(link["url"]).text
    assert "Prices include tax" in html and html.count("Prices include tax") == 1
    assert "VAT 17%" in html and "2.62" in html
    assert "Rounding" in html and "20.00" in html


def test_public_receipt_exclusive_has_no_included_note(http_client, t17):
    sj = sale(http_client, t17, qty="1", price="6", pay="7.02").json()
    link = http_client.post(t17.url(f"/sales/{sj['id']}/receipt-link"), headers=t17.owner_headers).json()
    html = http_client.get(link["url"]).text
    assert "Prices include tax" not in html and "Rounding" not in html and "GST 17%" in html


# ── sanity on the audit of recorded numbers ──────────────────────────────────


def test_sales_list_total_includes_rounding(http_client, t17):
    configure(http_client, t17, {"tax.rounding_mode": "nearest_5"})
    sj = sale(http_client, t17, qty="3", price="6", pay="20").json()
    r = http_client.get(t17.url("/sales"), headers=t17.owner_headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"] if isinstance(r.json(), dict) else r.json()
    row = next(i for i in items if i["id"] == sj["id"])
    assert D(row["total_amount"]) == D("20.00")
    assert D(db_scalar("SELECT rounding_amount FROM sales WHERE id = :i", {"i": sj["id"]})) == D("-1.06")
    assert put_setting(http_client, t17, "tax.label", "GST").status_code == 200
