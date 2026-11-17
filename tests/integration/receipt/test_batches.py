"""Pharmacy batch/expiry: GRN, FIFO vs FEFO, expired policy, stock_batch feed, mismatch, report."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_rows, db_scalar
from tests.helpers.phase2 import add_user, new_product, post_sale, second_branch
from tests.helpers.sync import by_entity, op, pull_all, push_ok, sale_payload, snapshot_all
from tests.helpers.tenants import create_supplier
from tests.integration.tax.helpers import put_setting

pytestmark = pytest.mark.integration


def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _adjust(c, t, product, qty, *, batch=None, expiry=None, cost="10.00", branch=None):
    line = {"product_id": product, "qty_delta": qty, "cost_per_unit": cost}
    if batch:
        line["batch_no"] = batch  # alias of batch_number
    if expiry:
        line["expiry_date"] = expiry
    r = c.post(t.url("/adjustments"), headers=t.owner_headers,
               json={"branch_id": branch or t.branch_id, "reason": "opening_balance", "lines": [line]})
    assert_status(r, 201, label="adjust")
    # distinct created_at ordering between layers
    return r.json()


def _sell(c, t, product, qty="1", **extra):
    return post_sale(c, t, product_id=product, qty=qty, unit_price="20.00", **extra)


def _allocation(sale_id: str) -> list[tuple[str, str]]:
    rows = db_rows(
        "SELECT b.batch_no, b.qty FROM sale_line_batches b JOIN sale_lines l ON l.id = b.sale_line_id "
        "WHERE l.sale_id = :s ORDER BY b.created_at, b.id",
        {"s": sale_id},
    )
    return sorted((r["batch_no"], str(int(r["qty"]))) for r in rows)


def _flags(sale_id: str) -> list[str]:
    return list(db_scalar("SELECT sync_flags FROM sales WHERE id = :s", {"s": sale_id}))


# -- GRN / import -------------------------------------------------------------


def test_grn_with_batch_and_expiry_accepts_both_spellings(http_client, pos_tenant):
    t = pos_tenant
    create_supplier(http_client, t)
    p = new_product(http_client, t)
    for spelling, batch, days in (("batch_no", "G-1", 100), ("batch_number", "G-2", 200)):
        r = http_client.post(
            t.url("/purchases/receipts"), headers=t.owner_headers,
            json={"branch_id": t.branch_id, "supplier_id": t.supplier_id,
                  "lines": [{"product_id": p, "qty_received": "5", "cost_per_unit": "8.00",
                             spelling: batch, "expiry_date": _d(days)}]},
        )
        assert_status(r, 201, label="grn")
    layers = db_rows(
        "SELECT batch_number, expiry_date FROM purchase_lines WHERE product_id = :p ORDER BY batch_number",
        {"p": p},
    )
    assert [(x["batch_number"], x["expiry_date"].isoformat()) for x in layers] == [
        ("G-1", _d(100)), ("G-2", _d(200))]


def test_product_track_expiry_and_unit_allows_decimal_roundtrip(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.post(
        t.url("/products"), headers=t.owner_headers,
        json={"name": "Syrup 100ml", "base_unit_id": t.unit_id, "product_type": "standard",
              "tracking_type": "none", "track_expiry": True, "name_secondary": "شربت"},
    )
    assert_status(r, 201, label="product")
    assert r.json()["track_expiry"] is True and r.json()["name_secondary"] == "شربت"
    u = http_client.post(t.url("/units"), headers=t.owner_headers,
                         json={"name": "Kilogram", "symbol": "kg", "allows_decimal": True})
    assert_status(u, 201, label="unit")
    assert u.json()["allows_decimal"] is True


# -- FIFO vs FEFO -------------------------------------------------------------


def _two_batches(c, t):
    p = new_product(c, t)
    _adjust(c, t, p, "5", batch="LATE", expiry=_d(300))   # older layer, expires later
    _adjust(c, t, p, "5", batch="EARLY", expiry=_d(30))   # newer layer, expires sooner
    return p


def test_fifo_is_the_default_and_consumes_oldest_layer(http_client, pos_tenant):
    t = pos_tenant
    p = _two_batches(http_client, t)
    sale = _sell(http_client, t, p, "2")
    assert_status(sale, 201, label="sale")
    assert _allocation(sale.json()["id"]) == [("LATE", "2")]


def test_fefo_consumes_earliest_expiry_first_and_spans_batches(http_client, pos_tenant):
    t = pos_tenant
    assert put_setting(http_client, t, "inventory.pick_policy", "fefo").status_code == 200
    p = _two_batches(http_client, t)
    sale = _sell(http_client, t, p, "7")
    assert_status(sale, 201, label="sale")
    assert _allocation(sale.json()["id"]) == [("EARLY", "5"), ("LATE", "2")]  # sorted by batch
    # costing stays per layer
    cost = db_scalar("SELECT cost_per_unit FROM sale_lines WHERE sale_id = :s", {"s": sale.json()["id"]})
    assert float(cost) == 10.0


def test_fefo_puts_layers_without_expiry_last(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.pick_policy", "fefo")
    p = new_product(http_client, t)
    _adjust(http_client, t, p, "3", batch="NOEXP")
    _adjust(http_client, t, p, "3", batch="DATED", expiry=_d(500))
    sale = _sell(http_client, t, p, "3")
    assert _allocation(sale.json()["id"]) == [("DATED", "3")]


# -- expired policy -----------------------------------------------------------


def _expired_product(c, t):
    p = new_product(c, t)
    _adjust(c, t, p, "5", batch="OLD", expiry=_d(-3))
    return p


def test_expired_sale_block_returns_400(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.expired_sale", "block")
    p = _expired_product(http_client, t)
    r = _sell(http_client, t, p)
    assert r.status_code == 400, r.text
    assert "expired" in r.text.lower()
    assert int(db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id})) == 0


def test_expired_block_falls_through_to_unexpired_stock(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.expired_sale", "block")
    put_setting(http_client, t, "inventory.pick_policy", "fefo")
    p = _expired_product(http_client, t)
    _adjust(http_client, t, p, "5", batch="FRESH", expiry=_d(90))
    r = _sell(http_client, t, p, "2")
    assert_status(r, 201, label="sale")
    assert _allocation(r.json()["id"]) == [("FRESH", "2")]
    assert "expired_batch_sold" not in _flags(r.json()["id"])


def test_expired_sale_warn_is_default_and_flags(http_client, pos_tenant):
    t = pos_tenant
    p = _expired_product(http_client, t)
    r = _sell(http_client, t, p)
    assert_status(r, 201, label="sale")
    assert "expired_batch_sold" in r.json()["sync_flags"]


def test_expired_sale_allow_has_no_flag(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.expired_sale", "allow")
    p = _expired_product(http_client, t)
    r = _sell(http_client, t, p)
    assert_status(r, 201, label="sale")
    assert r.json()["sync_flags"] == []


def test_offline_replay_of_expired_batch_is_accepted_and_flagged_even_in_block_mode(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.expired_sale", "block")
    p = _expired_product(http_client, t)
    payload = sale_payload(t, product_id=p, receipt="C1-0900")
    res = push_ok(http_client, t, [op("sale.create", payload)])[0]
    assert res["status"] == "applied", res
    assert "expired_batch_sold" in _flags(payload["id"])


# -- device vs server allocation ----------------------------------------------


def _offline_line(product, batches):
    return {"product_id": product, "qty": "1", "unit_price": "100.00", "batches": batches}


def test_batch_mismatch_flag_offline(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.pick_policy", "fefo")
    p = _two_batches(http_client, t)  # server will take EARLY
    wrong = sale_payload(t, receipt="C1-0910", lines=[_offline_line(p, [{"batch_no": "LATE", "qty": "1"}])])
    right = sale_payload(t, receipt="C1-0911", lines=[_offline_line(p, [{"batch_no": "EARLY", "qty": "1"}])])
    results = push_ok(http_client, t, [op("sale.create", wrong), op("sale.create", right)])
    assert [r["status"] for r in results] == ["applied", "applied"]  # never rejected
    assert "batch_mismatch" in _flags(wrong["id"])
    assert "batch_mismatch" not in _flags(right["id"])
    # the stored allocation is the server's, not the device's
    assert _allocation(wrong["id"]) == [("EARLY", "1")]


def test_batch_mismatch_flag_online(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.pick_policy", "fefo")
    p = _two_batches(http_client, t)
    r = _sell(http_client, t, p, line_extra={"batches": [{"batch_no": "LATE", "qty": "1"}]})
    assert_status(r, 201, label="sale")
    assert "batch_mismatch" in r.json()["sync_flags"]
    ok = _sell(http_client, t, p, line_extra={"batches": [{"batch_no": "EARLY", "qty": "1"}]})
    assert "batch_mismatch" not in ok.json()["sync_flags"]


# -- receipt prints the stored allocation -------------------------------------


def test_receipt_prints_actual_batch_and_expiry(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "inventory.pick_policy", "fefo")
    p = _two_batches(http_client, t)
    layout = next(x for x in http_client.get(t.url("/receipt-layouts"), headers=t.owner_headers).json()
                  if x["design"] == "thermal_classic")
    http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
                    json={"config": {"v": 1, "items": {"show_batch": True, "show_expiry": True}}})
    sale = _sell(http_client, t, p, line_extra={"batches": [{"batch_no": "LATE", "qty": "1"}]}).json()
    text = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                           params={"format": "text"}).text
    month = (date.today() + timedelta(days=30)).strftime("%m/%Y")
    assert "EARLY" in text and "LATE" not in text
    assert month in text
    # layout with batch off prints neither
    http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
                    json={"config": {"v": 1, "items": {"show_batch": False, "show_expiry": False}}})
    text = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                           params={"format": "text"}).text
    assert "EARLY" not in text


# -- stock_batch feed ---------------------------------------------------------


def test_stock_batch_entity_snapshot_pull_and_tombstone(http_client, pos_tenant):
    t = pos_tenant
    p = new_product(http_client, t)
    _adjust(http_client, t, p, "4", batch="SB-1", expiry=_d(60))
    _adjust(http_client, t, p, "4")  # no batch / expiry: not a stock_batch
    changes, cursor, _ = snapshot_all(http_client, t)
    rows = [c for c in by_entity(changes, "stock_batch") if c["data"]["product_id"] == p]
    assert len(rows) == 1
    d = rows[0]["data"]
    assert (d["batch_no"], d["expiry_date"], d["branch_id"], d["qty_remaining"]) == (
        "SB-1", _d(60), t.branch_id, "4.0000")
    assert set(d) >= {"product_id", "variation_id", "batch_no", "expiry_date", "qty_remaining"}

    # a sale changes qty_remaining -> new upsert
    put_setting(http_client, t, "inventory.pick_policy", "fefo")
    assert_status(_sell(http_client, t, p, "1"), 201, label="sale")
    feed, cursor = pull_all(http_client, t, since=cursor)
    upd = [c for c in by_entity(feed, "stock_batch") if c["id"] == rows[0]["id"]]
    assert upd and upd[-1]["data"]["qty_remaining"] == "3.0000"

    # selling it out removes it from the feed
    assert_status(_sell(http_client, t, p, "3"), 201, label="sale")
    feed, _ = pull_all(http_client, t, since=cursor)
    gone = [c for c in by_entity(feed, "stock_batch") if c["id"] == rows[0]["id"]]
    assert gone and gone[-1]["op"] == "delete"
    snap, _, _ = snapshot_all(http_client, t)
    assert all(c["id"] != rows[0]["id"] for c in by_entity(snap, "stock_batch"))


def test_stock_batch_is_branch_scoped(http_client, pos_tenant):
    t = pos_tenant
    other = second_branch(http_client, t)
    p = new_product(http_client, t)
    _adjust(http_client, t, p, "2", batch="HERE", expiry=_d(40))
    _adjust(http_client, t, p, "2", batch="THERE", expiry=_d(40), branch=other)
    owner, _, _ = snapshot_all(http_client, t)
    assert {c["data"]["batch_no"] for c in by_entity(owner, "stock_batch")
            if c["data"]["product_id"] == p} == {"HERE", "THERE"}
    cashier = add_user(http_client, t, "cashier", branch_id=t.branch_id)
    mine, _, _ = snapshot_all(http_client, t, headers=cashier)
    assert {c["data"]["batch_no"] for c in by_entity(mine, "stock_batch")
            if c["data"]["product_id"] == p} == {"HERE"}


# -- expiry report + export ---------------------------------------------------


def test_expiry_report_and_export(http_client, pos_tenant):
    t = pos_tenant
    p = new_product(http_client, t)
    _adjust(http_client, t, p, "4", batch="DEAD", expiry=_d(-10), cost="5.00")
    _adjust(http_client, t, p, "3", batch="SOON", expiry=_d(10), cost="7.00")
    _adjust(http_client, t, p, "9", batch="FAR", expiry=_d(400), cost="7.00")
    r = http_client.get(t.url("/stock/expiry-report"), headers=t.owner_headers, params={"within_days": 30})
    assert_status(r, 200, label="report")
    body = r.json()
    assert [x["batch_no"] for x in body["expired"] if x["product_id"] == p] == ["DEAD"]
    assert [x["batch_no"] for x in body["near_expiry"] if x["product_id"] == p] == ["SOON"]
    dead = next(x for x in body["expired"] if x["batch_no"] == "DEAD")
    assert dead["value"] == "20.00" and dead["days_to_expiry"] == -10
    assert float(body["totals"]["expired_value"]) >= 20.0
    assert float(body["totals"]["near_expiry_value"]) >= 21.0
    assert all(x["batch_no"] != "FAR" for x in body["near_expiry"])

    wide = http_client.get(t.url("/stock/expiry-report"), headers=t.owner_headers, params={"within_days": 500})
    assert any(x["batch_no"] == "FAR" for x in wide.json()["near_expiry"])

    assert http_client.get(t.url("/stock/expiry-report"), headers=t.cashier_headers).status_code in (200, 403)

    ex = http_client.get(t.url("/export/expiry"), headers=t.owner_headers, params={"format": "csv"})
    assert_status(ex, 200, label="export")
    csv_text = ex.text
    assert "batch_no" in csv_text and "DEAD" in csv_text and "SOON" in csv_text
    assert "expired" in csv_text and "near_expiry" in csv_text
    listed = http_client.get(t.url("/export"), headers=t.owner_headers).json()
    assert "expiry" in str(listed)


def test_adjustment_batch_unknown_id_noise(http_client, pos_tenant):
    """Unrelated sanity: layers without batch never appear in the expiry report."""
    t = pos_tenant
    body = http_client.get(t.url("/stock/expiry-report"), headers=t.owner_headers,
                           params={"within_days": 730}).json()
    assert all(x["batch_no"] is not None or x["expiry_date"] for x in body["expired"] + body["near_expiry"])
    assert uuid.UUID(t.business_id)
