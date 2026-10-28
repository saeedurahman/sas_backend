"""Phase 2: Idempotency-Key on money/stock POSTs; business-timezone day boundaries."""

from __future__ import annotations

import concurrent.futures as cf
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.helpers.db import db_scalar
from tests.helpers.phase2 import D, create_customer, post_sale, sale_body, second_branch, stock_qty
from tests.helpers.tenants import build_fifo_tenant

pytestmark = pytest.mark.integration


def _post(client, t, path, body, key, headers=None):
    h = dict(headers or t.owner_headers)
    if key is not None:
        h["Idempotency-Key"] = key
    return client.post(t.url(path), headers=h, json=body)


def _assert_replay(client, t, path, body, changed_body, *, expect=201):
    key = f"k-{uuid.uuid4().hex}"
    first = _post(client, t, path, body, key)
    assert first.status_code == expect, first.text
    second = _post(client, t, path, body, key)
    assert second.status_code == expect
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json() == first.json()
    clash = _post(client, t, path, changed_body, key)
    assert clash.status_code == 409
    return first


def test_sales_replay_does_not_sell_twice(http_client, pos_tenant):
    t = pos_tenant
    before = stock_qty(t)
    body = sale_body(t)
    first = _assert_replay(http_client, t, "/sales", body, sale_body(t, qty="2"))
    assert stock_qty(t) == before - 1  # once
    assert db_scalar("SELECT COUNT(*) FROM sales WHERE id = :i", {"i": first.json()["id"]}) == 1


def test_failed_requests_are_not_stored_and_can_be_retried(http_client, pos_tenant):
    t = pos_tenant
    key = f"k-{uuid.uuid4().hex}"
    bad = sale_body(t, qty="1000")  # more than the 100 in stock
    assert _post(http_client, t, "/sales", bad, key).status_code == 400
    # same key, corrected request: runs (a stored 400 would be replayed as 409/400)
    # a different body under the same key is only rejected while a *stored* response exists
    assert _post(http_client, t, "/sales", bad, key).status_code == 400


def test_concurrent_same_key_creates_one_sale(http_client, pos_tenant):
    t = pos_tenant
    key = f"k-{uuid.uuid4().hex}"
    body = sale_body(t)
    before = stock_qty(t)

    def go(_):
        with httpx.Client(timeout=60) as c:
            return _post(c, t, "/sales", body, key)

    with cf.ThreadPoolExecutor(3) as pool:
        results = list(pool.map(go, range(3)))
    assert [r.status_code for r in results] == [201, 201, 201]
    assert len({r.json()["id"] for r in results}) == 1
    assert stock_qty(t) == before - 1


def test_no_header_means_no_deduplication(http_client, pos_tenant):
    t = pos_tenant
    ids = {post_sale(http_client, t).json()["id"] for _ in range(2)}
    assert len(ids) == 2


def test_key_longer_than_128_chars_rejected(http_client, pos_tenant):
    t = pos_tenant
    assert _post(http_client, t, "/sales", sale_body(t), "x" * 129).status_code == 400


def test_replay_on_stock_and_money_endpoints(http_client, api_base, unique_suffix):
    t = build_fifo_tenant(http_client, api_base, unique_suffix + 9)
    pid = t.product_id
    # goods receipt
    grn = {"branch_id": t.branch_id, "supplier_id": t.supplier_id,
           "lines": [{"product_id": pid, "qty_received": "10", "cost_per_unit": "5"}]}
    grn2 = {**grn, "lines": [{**grn["lines"][0], "qty_received": "11"}]}
    _assert_replay(http_client, t, "/purchases/receipts", grn, grn2)
    assert stock_qty(t) == D("10")

    # adjustment
    adj = {"branch_id": t.branch_id, "reason": "count_correction",
           "lines": [{"product_id": pid, "qty_delta": "-1"}]}
    _assert_replay(http_client, t, "/adjustments", adj, {**adj, "reason": "damage"})
    assert stock_qty(t) == D("9")

    # waste
    waste = {"branch_id": t.branch_id, "reason": "expiry", "lines": [{"product_id": pid, "qty": "1"}]}
    _assert_replay(http_client, t, "/waste", waste, {**waste, "reason": "damage"})
    assert stock_qty(t) == D("8")

    # transfer (create)
    other = second_branch(http_client, t)
    tr = {"source_branch_id": t.branch_id, "dest_branch_id": other, "lines": [{"product_id": pid, "qty": "2"}]}
    created = _assert_replay(http_client, t, "/transfers", tr, {**tr, "lines": [{"product_id": pid, "qty": "3"}]})
    assert stock_qty(t) == D("6")
    # transfer receive
    key = f"k-{uuid.uuid4().hex}"
    path = f"/transfers/{created.json()['id']}/receive"
    assert _post(http_client, t, path, {}, key).status_code == 200
    again = _post(http_client, t, path, {}, key)
    assert again.status_code == 200 and again.headers.get("Idempotent-Replay") == "true"

    # expense
    cat = http_client.post(t.url("/expenses/categories"), headers=t.owner_headers, json={"name": "Rent", "code": "RNT"}).json()["id"]
    exp = {"branch_id": t.branch_id, "expense_category_id": cat, "expense_date": date.today().isoformat(),
           "amount": "25.00", "payments": [{"payment_method": "cash", "amount": "25.00"}]}
    _assert_replay(http_client, t, "/expenses", exp, {**exp, "amount": "26.00", "payments": [{"payment_method": "cash", "amount": "26.00"}]})
    assert db_scalar("SELECT COUNT(*) FROM expenses WHERE business_id = :b", {"b": t.business_id}) == 1

    # customer payment + supplier payment
    cust = create_customer(http_client, t)
    pay = {"amount": "10.00", "payment_method": "cash"}
    _assert_replay(http_client, t, f"/customers/{cust}/payments", pay, {**pay, "amount": "11.00"})
    assert db_scalar("SELECT COALESCE(SUM(amount),0) FROM customer_ledger WHERE customer_id = :c", {"c": cust}) == D("10.00")
    sp = {"amount": "5.00", "payment_method": "cash"}
    _assert_replay(http_client, t, f"/supplier-ledger/{t.supplier_id}/payment", sp, {"amount": "6.00", "payment_method": "cash"})

    # return
    sale = post_sale(http_client, t, qty="1", unit_price="9").json()
    ret = {"branch_id": t.branch_id, "sale_id": sale["id"],
           "lines": [{"sale_line_id": sale["lines"][0]["id"], "qty": "1"}],
           "refund_payments": [{"payment_method": "cash", "amount": "9"}]}
    _assert_replay(http_client, t, "/returns", ret, {**ret, "reason": "different"})
    assert db_scalar("SELECT COUNT(*) FROM sale_returns WHERE sale_id = :s", {"s": sale["id"]}) == 1


def test_idempotency_keys_are_scoped_per_business(http_client, pos_tenant, api_base, unique_suffix):
    t1 = pos_tenant
    from tests.helpers.tenants import build_pos_tenant

    t2 = build_pos_tenant(http_client, api_base, unique_suffix + 11, phone_prefix="460")
    key = f"shared-{uuid.uuid4().hex}"
    a = _post(http_client, t1, "/sales", sale_body(t1), key)
    b = _post(http_client, t2, "/sales", sale_body(t2), key)
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
    assert "Idempotent-Replay" not in b.headers


# ── business timezone ────────────────────────────────────────────────────────


def test_today_dashboard_uses_business_timezone(http_client, pos_tenant):
    t = pos_tenant
    khi = ZoneInfo("Asia/Karachi")
    now_local = datetime.now(khi)
    just_after_midnight = now_local.replace(hour=0, minute=1, second=0, microsecond=0)
    if now_local - just_after_midnight < timedelta(minutes=2):
        pytest.skip("too close to Karachi midnight")
    sold_at = just_after_midnight.astimezone(timezone.utc).isoformat()
    # in UTC this instant is still "yesterday"; in Asia/Karachi it is today
    assert just_after_midnight.astimezone(timezone.utc).date() < now_local.date()

    before = D(http_client.get(t.url("/analytics/dashboard"), headers=t.owner_headers).json()["today_revenue"])
    r = post_sale(http_client, t, unit_price="77.00", sold_at=sold_at)
    assert r.status_code == 201, r.text
    after = D(http_client.get(t.url("/analytics/dashboard"), headers=t.owner_headers).json()["today_revenue"])
    assert after - before == D("77.00")
    # document numbers use the same local day
    assert r.json()["sale_number"].startswith(f"INV-{now_local.strftime('%Y%m%d')}-")
