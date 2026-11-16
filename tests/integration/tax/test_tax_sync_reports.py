"""Tax / price settings: offline replay flags, report consistency and export columns."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import pytest

from tests.helpers.db import db_rows
from tests.helpers.phase2 import D
from tests.helpers.sync import ago, op, pull_all, push_ok, sale_payload
from tests.integration.tax.helpers import configure, make_rate, sale, use_rate_on_product

pytestmark = pytest.mark.integration


@pytest.fixture
def t17(http_client, pos_tenant):
    use_rate_on_product(pos_tenant, make_rate(http_client, pos_tenant, "17"))
    return pos_tenant


def _replay(client, t, payload: dict, **op_kw) -> dict:
    return push_ok(client, t, [op("sale.create", payload, **op_kw)])[0]


def _stored(result) -> tuple[dict, dict]:
    sale_row = db_rows("SELECT * FROM sales WHERE id = :i", {"i": result["server_id"]})[0]
    line = db_rows("SELECT * FROM sale_lines WHERE sale_id = :i", {"i": result["server_id"]})[0]
    return sale_row, line


# ── sync replay ──────────────────────────────────────────────────────────────

MISMATCH_FLAGS = {"tax_mismatch", "rounding_mismatch", "total_mismatch"}


def test_replay_exclusive_matching_has_no_flags(http_client, t17):
    p = sale_payload(t17, qty="1", unit_price="6", tax_rate="17", tax_amount="1.02",
                     payments=[{"payment_method": "cash", "amount": "7.02"}])
    r = _replay(http_client, t17, p)
    assert r["status"] == "applied" and not (set(r["flags"]) & MISMATCH_FLAGS), r
    s, line = _stored(r)
    assert s["tax_inclusive"] is False and D(s["rounding_amount"]) == 0
    assert (D(line["net_amount"]), D(line["tax_amount"])) == (D("6.00"), D("1.02"))


def test_replay_inclusive_with_device_mode(http_client, t17):
    p = sale_payload(t17, qty="1", unit_price="6", tax_rate="17", tax_amount="0.87",
                     payments=[{"payment_method": "cash", "amount": "6.00"}])
    p["tax_inclusive"] = True
    r = _replay(http_client, t17, p)
    assert r["status"] == "applied" and not (set(r["flags"]) & MISMATCH_FLAGS), r
    s, line = _stored(r)
    assert s["tax_inclusive"] is True
    assert (D(line["net_amount"]), D(line["tax_amount"])) == (D("5.13"), D("0.87"))


def test_replay_wrong_tax_flags_but_never_rejects(http_client, t17):
    p = sale_payload(t17, qty="1", unit_price="6", tax_rate="17", tax_amount="9.99",
                     payments=[{"payment_method": "cash", "amount": "15.99"}])
    r = _replay(http_client, t17, p)
    assert r["status"] == "applied" and "tax_mismatch" in r["flags"]
    _, line = _stored(r)
    assert D(line["tax_amount"]) == D("9.99")  # the device figure is kept


def test_replay_rounding_ok_and_mismatch(http_client, t17):
    ok = sale_payload(t17, qty="3", unit_price="6", tax_rate="17", tax_amount="3.06",
                      payments=[{"payment_method": "cash", "amount": "20.00"}])
    ok.update({"tax_rounding_mode": "nearest_5", "rounding_amount": "-1.06", "total_amount": "20.00"})
    r = _replay(http_client, t17, ok)
    assert r["status"] == "applied" and not (set(r["flags"]) & MISMATCH_FLAGS), r
    s, _ = _stored(r)
    assert s["tax_rounding_mode"] == "nearest_5" and D(s["rounding_amount"]) == D("-1.06")

    bad = sale_payload(t17, qty="3", unit_price="6", tax_rate="17", tax_amount="3.06",
                       payments=[{"payment_method": "cash", "amount": "21.00"}])
    bad.update({"tax_rounding_mode": "nearest_5", "rounding_amount": "-0.06", "total_amount": "21.00"})
    r = _replay(http_client, t17, bad)
    assert r["status"] == "applied"
    assert {"rounding_mismatch"} <= set(r["flags"]) or "total_mismatch" in r["flags"], r


def test_replay_without_mode_uses_settings_in_effect_at_occurred_at(http_client, t17):
    # device did not send a mode: the settings valid when the sale happened apply, not today's
    before_change = ago(minutes=5)
    configure(http_client, t17, {"tax.prices_include_tax": True})
    p = sale_payload(t17, qty="1", unit_price="6", tax_rate="17", tax_amount="1.02",
                     payments=[{"payment_method": "cash", "amount": "7.02"}])
    r = _replay(http_client, t17, p, occurred_at=before_change)
    assert r["status"] == "applied"
    s, line = _stored(r)
    assert s["tax_inclusive"] is False  # exclusive at that time
    assert D(line["tax_amount"]) == D("1.02")
    assert "settings_mismatch" in r["flags"]  # differs from the current settings


# ── report consistency ───────────────────────────────────────────────────────


def _range():
    today = date.today()
    return {"date_from": str(today - timedelta(days=2)), "date_to": str(today + timedelta(days=2))}


def _csv(client, t, dataset: str) -> list[dict]:
    r = client.get(t.url(f"/export/{dataset}"), headers=t.owner_headers, params={**_range(), "format": "csv"})
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _col(rows: list[dict], name: str) -> list[str]:
    key = next(k for k in rows[0] if k.strip().lower() == name)
    return [r[key] for r in rows]


@pytest.mark.parametrize("inclusive", [False, True])
def test_revenue_matches_across_summary_pnl_dashboard_and_export(http_client, t17, inclusive):
    configure(http_client, t17, {"tax.prices_include_tax": inclusive, "tax.rounding_mode": "nearest_5"})
    for qty, pay in (("3", "25"), ("1", "10"), ("2", "20")):
        assert sale(http_client, t17, qty=qty, price="6", pay=pay).status_code == 201

    exp_rows = db_rows(
        "SELECT SUM(sl.net_amount) AS net, SUM(sl.tax_amount) AS tax FROM sale_lines sl "
        "JOIN sales s ON s.id = sl.sale_id WHERE s.business_id = :b AND s.status = 'completed'",
        {"b": t17.business_id},
    )[0]
    rounding = D(db_rows("SELECT SUM(rounding_amount) AS r FROM sales WHERE business_id = :b", {"b": t17.business_id})[0]["r"])
    net, tax = D(exp_rows["net"]), D(exp_rows["tax"])

    summary = http_client.get(t17.url("/analytics/sales-summary"), headers=t17.owner_headers, params=_range()).json()
    pnl = http_client.get(t17.url("/analytics/profit-loss"), headers=t17.owner_headers, params=_range()).json()
    dash = http_client.get(t17.url("/analytics/dashboard"), headers=t17.owner_headers).json()
    assert D(summary["total_revenue"]) == net  # revenue = net sales
    assert D(pnl["total_revenue"]) == net
    assert D(dash["this_month_revenue"]) == net or D(dash["today_revenue"]) == net
    assert D(summary["total_tax"]) == tax
    assert D(summary["total_rounding"]) == rounding
    assert D(summary["total_gross"]) == net + tax
    assert D(summary["total_collected"]) == net + tax + rounding

    sales_rows = _csv(http_client, t17, "sales")
    assert sum(D(x) for x in _col(sales_rows, "net")) == net
    assert sum(D(x) for x in _col(sales_rows, "tax")) == tax
    assert sum(D(x) for x in _col(sales_rows, "rounding")) == rounding
    assert sum(D(x) for x in _col(sales_rows, "total")) == net + tax + rounding

    lines = _csv(http_client, t17, "sale_lines")
    assert sum(D(x) for x in _col(lines, "net")) == net
    assert sum(D(x) for x in _col(lines, "line_total")) == net + tax


def test_export_dataset_columns_listed(http_client, t17):
    r = http_client.get(t17.url("/export"), headers=t17.owner_headers)
    assert r.status_code == 200, r.text
    cols = {d["name"]: d["columns"] for d in r.json()["datasets"]}
    assert {"net", "tax", "rounding", "total", "tax_mode", "tax_label"} <= set(cols["sales"])
    assert {"net", "tax_rate", "tax", "tax_label", "line_total"} <= set(cols["sale_lines"])
    assert "tax_label" in cols["tax_summary"]


def test_exports_in_all_formats(http_client, t17):
    configure(http_client, t17, {"tax.rounding_mode": "nearest_5"})
    assert sale(http_client, t17, qty="3", price="6", pay="20").status_code == 201
    for fmt in ("csv", "xlsx", "pdf"):
        r = http_client.get(t17.url("/export/sales"), headers=t17.owner_headers, params={**_range(), "format": fmt})
        assert r.status_code == 200, (fmt, r.text[:200])
        assert len(r.content) > 100


def test_legacy_sales_export_has_net_and_rounding(http_client, t17):
    configure(http_client, t17, {"tax.rounding_mode": "nearest_5"})
    assert sale(http_client, t17, qty="3", price="6", pay="20").status_code == 201
    r = http_client.get(t17.url("/invoice/export/sales"), headers=t17.owner_headers, params={**_range(), "format": "csv"})
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8-sig")
    header = text.splitlines()[0].lower()
    assert "net" in header and "rounding" in header
    assert "20.00" in text


def test_tax_summary_groups_by_label_and_rate(http_client, t17):
    configure(http_client, t17, {"tax.label": "VAT"})
    assert sale(http_client, t17, qty="1", price="6", pay="7.02").status_code == 201
    r = http_client.get(t17.url("/analytics/tax-summary"), headers=t17.owner_headers, params=_range())
    assert r.status_code == 200, r.text
    groups = r.json()["tax_by_rate"]
    assert any(g["tax_label"] == "VAT" and D(g["tax_rate"]) == D("17") for g in groups), groups


def test_sync_feed_sale_summary_carries_tax_columns(http_client, t17):
    configure(http_client, t17, {"tax.prices_include_tax": True, "tax.rounding_mode": "nearest_5"})
    sj = sale(http_client, t17, qty="3", price="6", pay="20").json()
    changes, _ = pull_all(http_client, t17)
    ent = [c for c in changes if c["entity"] == "sale_summary" and c["id"] == sj["id"]]
    assert ent, sorted({c["entity"] for c in changes})
    data = ent[0]["data"]
    assert data["tax_inclusive"] is True and data["tax_rounding_mode"] == "nearest_5"
    assert D(data["rounding_amount"]) == D("2.00") and data["tax_label"] == "GST"
    totals = data["totals"]
    assert D(totals["net"]) + D(totals["tax"]) == D(totals["exact_total"]) == D("18.00")
    assert D(totals["rounding"]) == D("2.00") and D(totals["total"]) == D("20.00")
