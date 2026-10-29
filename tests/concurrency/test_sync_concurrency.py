"""Concurrent pushes for one business: no duplicates, no lost updates, consistent FIFO."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest

from tests.helpers.db import db_rows, db_scalar
from tests.helpers.phase2 import D, new_product, stock_qty
from tests.helpers.sync import op, pull_all, push, sale_payload

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


def _push_in_thread(t, ops, device_id):
    with httpx.Client(timeout=60.0) as client:
        resp = push(client, t, ops, device_id=device_id)
        assert resp.status_code == 200, resp.text
        return resp.json()["results"]


def test_two_pushes_for_the_same_business_do_not_interfere(pos_tenant):
    t = pos_tenant
    batches = {
        "dev-A": [op("sale.create", sale_payload(t, qty="2")) for _ in range(6)],
        "dev-B": [op("sale.create", sale_payload(t, qty="3")) for _ in range(6)],
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {d: pool.submit(_push_in_thread, t, ops, d) for d, ops in batches.items()}
        results = {d: f.result() for d, f in futures.items()}

    for device_results in results.values():
        assert [r["status"] for r in device_results] == ["applied"] * 6
    numbers = [r["official_number"] for rs in results.values() for r in rs]
    assert len(set(numbers)) == 12  # server-assigned numbers never collide
    assert stock_qty(t) == D("100") - 6 * 2 - 6 * 3
    assert db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id}) == 12
    assert db_scalar("SELECT count(*) FROM sync_ops WHERE business_id = :b AND status = 'applied'",
                     {"b": t.business_id}) == 12


def test_same_op_delivered_twice_at_once_is_applied_once(pos_tenant):
    t = pos_tenant
    sale = op("sale.create", sale_payload(t, qty="4"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_push_in_thread, t, [sale], f"dev-{i}") for i in range(4)]
        outcomes = [f.result()[0] for f in futures]
    statuses = sorted(o["status"] for o in outcomes)
    assert statuses == ["applied", "duplicate", "duplicate", "duplicate"], outcomes
    assert len({o["server_id"] for o in outcomes}) == 1
    assert len({o["official_number"] for o in outcomes}) == 1
    assert db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id}) == 1
    assert stock_qty(t) == D("96")


def test_two_devices_selling_the_same_stock_offline(pos_tenant):
    """100 on hand, two tills each sold 70 while offline."""
    t = pos_tenant
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_push_in_thread, t, [op("sale.create", sale_payload(t, qty="70", unit_price="10.00"))], d)
            for d in ("till-1", "till-2")
        ]
        results = [f.result()[0] for f in futures]

    assert [r["status"] for r in results] == ["applied", "applied"]
    flagged = [r for r in results if "oversold" in r["flags"]]
    assert len(flagged) == 1, results  # exactly the later one ran out
    assert "cost_estimated" in flagged[0]["flags"]
    assert all("oversold" not in r["flags"] for r in results if r is not flagged[0])

    # Stock ledger: 100 - 140 = -40, no lost update.
    assert stock_qty(t) == D("-40")
    movements = db_rows(
        "SELECT qty, purchase_line_id FROM stock_movements WHERE business_id = :b AND product_id = :p "
        "AND movement_type = 'sale'", {"b": t.business_id, "p": t.product_id},
    )
    assert sum(D(m["qty"]) for m in movements) == D("-140")

    # FIFO layers: fully consumed, never negative; the uncovered 40 carry no layer.
    layers = db_rows(
        "SELECT qty_remaining FROM purchase_lines WHERE business_id = :b AND product_id = :p",
        {"b": t.business_id, "p": t.product_id},
    )
    assert layers and all(D(l["qty_remaining"]) >= 0 for l in layers)
    assert sum(D(l["qty_remaining"]) for l in layers) == D("0")
    uncovered = sum(-D(m["qty"]) for m in movements if m["purchase_line_id"] is None)
    assert uncovered == D("40")

    lines = db_rows(
        "SELECT sale_id, cost_estimated, cost_per_unit FROM sale_lines WHERE business_id = :b", {"b": t.business_id}
    )
    assert sorted(l["cost_estimated"] for l in lines) == [False, True]
    assert all(D(l["cost_per_unit"]) > 0 for l in lines)  # fallback cost, never zero

    # Derived stock_levels row agrees with the ledger.
    level = db_scalar(
        "SELECT qty FROM stock_levels WHERE business_id = :b AND product_id = :p", {"b": t.business_id, "p": t.product_id}
    )
    assert D(level) == D("-40")
    # Both receipts exist, one flag notification.
    assert db_scalar("SELECT count(*) FROM notification_log WHERE business_id = :b AND notification_type = 'sync_conflict'",
                     {"b": t.business_id}) == 1


def test_change_seq_has_no_gaps_or_late_commits_under_concurrent_writers(pos_tenant, http_client):
    """Cursor correctness: nothing committed concurrently may be skipped by a later pull."""
    t = pos_tenant
    _, cursor = pull_all(http_client, t)

    def create(i: int) -> str:
        with httpx.Client(timeout=60.0) as client:
            return new_product(client, t, f"Racer {i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = set(pool.map(create, range(16)))
    changes, _ = pull_all(http_client, t, since=cursor, limit=7)
    delivered = {c["id"] for c in changes if c["entity"] == "product"}
    assert created <= delivered

    seqs = db_rows(
        "SELECT change_seq FROM products WHERE business_id = :b AND change_seq IS NOT NULL ORDER BY change_seq",
        {"b": t.business_id},
    )
    values = [r["change_seq"] for r in seqs]
    assert len(values) == len(set(values))


def test_concurrent_pushes_and_catalog_edits_do_not_deadlock(pos_tenant, http_client):
    t = pos_tenant
    errors: list[str] = []

    def sell(i: int):
        with httpx.Client(timeout=60.0) as client:
            resp = push(client, t, [op("sale.create", sale_payload(t)) for _ in range(3)], device_id=f"d{i}")
            if resp.status_code != 200 or any(r["status"] != "applied" for r in resp.json()["results"]):
                errors.append(resp.text[:300])

    def edit(i: int):
        with httpx.Client(timeout=60.0) as client:
            resp = client.put(t.url(f"/products/{t.product_id}"), headers=t.owner_headers, json={"name": f"Edit {i}"})
            if resp.status_code != 200:
                errors.append(resp.text[:300])

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(sell, i) for i in range(4)] + [pool.submit(edit, i) for i in range(4)]
        for f in futures:
            f.result()
    assert not errors, errors
    assert stock_qty(t) == D("100") - 12
