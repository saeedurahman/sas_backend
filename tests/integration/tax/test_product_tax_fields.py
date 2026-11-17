"""Product tax rate / tax-exempt fields: API, validation, audit, sync feed, CSV import/export."""

from __future__ import annotations

import csv
import io
import uuid

import pytest

from tests.helpers.db import db_execute, db_rows
from tests.helpers.phase2 import D, new_product
from tests.helpers.sync import snapshot_all
from tests.helpers.tenants import build_pos_tenant
from tests.integration.tax.helpers import configure, make_rate, sale

pytestmark = pytest.mark.integration


def _put(client, t, pid, body, headers=None):
    return client.put(t.url(f"/products/{pid}"), headers=headers or t.owner_headers, json=body)


def _get(client, t, pid) -> dict:
    r = client.get(t.url(f"/products/{pid}"), headers=t.owner_headers)
    assert r.status_code == 200, r.text
    return r.json()


def _create(client, t, **extra) -> dict:
    r = client.post(
        t.url("/products"),
        headers=t.owner_headers,
        json={
            "name": f"P{uuid.uuid4().hex[:6]}",
            "base_unit_id": t.unit_id,
            "product_type": "standard",
            "tracking_type": "none",
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── API: set / clear / read ──────────────────────────────────────────────────


def test_defaults_on_create_and_read(http_client, pos_tenant):
    p = _create(http_client, pos_tenant)
    assert (p["tax_rate_id"], p["tax_rate"], p["is_tax_exempt"]) == (None, None, False)


def test_create_with_rate_and_exempt(http_client, pos_tenant):
    t = pos_tenant
    rid = make_rate(http_client, t, "17")
    p = _create(http_client, t, tax_rate_id=rid, is_tax_exempt=True)
    assert p["tax_rate_id"] == rid and D(p["tax_rate"]) == D("17") and p["is_tax_exempt"] is True
    got = _get(http_client, t, p["id"])
    assert got["tax_rate_id"] == rid and D(got["tax_rate"]) == D("17") and got["is_tax_exempt"] is True


def test_set_and_clear_via_update(http_client, pos_tenant):
    t = pos_tenant
    pid = new_product(http_client, t)
    rid = make_rate(http_client, t, "5")
    r = _put(http_client, t, pid, {"tax_rate_id": rid, "is_tax_exempt": True})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["tax_rate_id"], D(b["tax_rate"]), b["is_tax_exempt"]) == (rid, D("5"), True)

    # omitting the field leaves it alone
    assert _put(http_client, t, pid, {"name": "Renamed"}).json()["tax_rate_id"] == rid
    # explicit null clears the rate; exempt can be switched off separately
    b = _put(http_client, t, pid, {"tax_rate_id": None}).json()
    assert (b["tax_rate_id"], b["tax_rate"], b["is_tax_exempt"]) == (None, None, True)
    assert _put(http_client, t, pid, {"is_tax_exempt": False}).json()["is_tax_exempt"] is False


def test_list_carries_both_fields(http_client, pos_tenant):
    t = pos_tenant
    rid = make_rate(http_client, t, "10")
    p = _create(http_client, t, tax_rate_id=rid, is_tax_exempt=True)
    items = http_client.get(t.url("/products"), headers=t.owner_headers, params={"limit": 200}).json()["items"]
    row = next(i for i in items if i["id"] == p["id"])
    assert row["tax_rate_id"] == rid and row["is_tax_exempt"] is True


# ── validation ───────────────────────────────────────────────────────────────


def test_other_business_rate_rejected(http_client, pos_tenant, api_base, unique_suffix):
    other = build_pos_tenant(http_client, api_base, unique_suffix + 1, phone_prefix="441")
    foreign = make_rate(http_client, other, "17")
    t = pos_tenant
    pid = new_product(http_client, t)
    assert _put(http_client, t, pid, {"tax_rate_id": foreign}).status_code == 400
    assert _put(http_client, t, pid, {"tax_rate_id": str(uuid.uuid4())}).status_code == 400
    r = http_client.post(
        t.url("/products"), headers=t.owner_headers,
        json={"name": "Nope", "base_unit_id": t.unit_id, "product_type": "standard",
              "tracking_type": "none", "tax_rate_id": foreign},
    )
    assert r.status_code == 400
    assert _get(http_client, t, pid)["tax_rate_id"] is None


def test_inactive_rate_rejected(http_client, pos_tenant):
    t = pos_tenant
    rid = make_rate(http_client, t, "17")
    db_execute("UPDATE tax_rates SET is_active = FALSE WHERE id = :i", {"i": rid})
    assert _put(http_client, t, new_product(http_client, t), {"tax_rate_id": rid}).status_code == 400


def test_permission_products_update_required(http_client, pos_tenant):
    t = pos_tenant
    pid = new_product(http_client, t)
    r = _put(http_client, t, pid, {"is_tax_exempt": True}, headers=t.cashier_headers)
    assert r.status_code == 403
    assert _get(http_client, t, pid)["is_tax_exempt"] is False


# ── audit ────────────────────────────────────────────────────────────────────


def test_audit_before_after(http_client, pos_tenant):
    t = pos_tenant
    pid = new_product(http_client, t)
    rid = make_rate(http_client, t, "17")
    _put(http_client, t, pid, {"tax_rate_id": rid, "is_tax_exempt": True})
    _put(http_client, t, pid, {"name": "Only a rename"})  # no tax change: no audit row
    _put(http_client, t, pid, {"tax_rate_id": None})
    rows = db_rows(
        "SELECT old_values, new_values, user_id FROM audit_logs WHERE business_id = :b "
        "AND table_name = 'products' AND record_id = :p AND new_values ? 'is_tax_exempt' "
        "ORDER BY created_at",
        {"b": t.business_id, "p": pid},
    )
    assert len(rows) == 2
    assert rows[0]["old_values"] == {"tax_rate_id": None, "is_tax_exempt": False}
    assert rows[0]["new_values"] == {"tax_rate_id": rid, "is_tax_exempt": True}
    assert rows[1]["old_values"]["tax_rate_id"] == rid
    assert rows[1]["new_values"] == {"tax_rate_id": None, "is_tax_exempt": True}
    assert rows[0]["user_id"] is not None


# ── exempt product in a sale, both modes ─────────────────────────────────────


@pytest.mark.parametrize("inclusive", [False, True])
def test_exempt_product_gets_zero_tax_via_api(http_client, pos_tenant, inclusive):
    t = pos_tenant
    rid = make_rate(http_client, t, "17")
    configure(http_client, t, {"tax.prices_include_tax": inclusive, "tax.default_rate_id": rid})
    assert _put(http_client, t, t.product_id, {"tax_rate_id": rid, "is_tax_exempt": True}).status_code == 200
    r = sale(http_client, t, qty="3", price="6", pay="18")
    assert r.status_code == 201, r.text
    b = r.json()
    assert D(b["total_tax"]) == 0 and D(b["total_amount"]) == D("18.00") and D(b["net_amount"]) == D("18.00")
    assert D(b["lines"][0]["tax_rate"]) == 0

    # switching exemption off makes the product taxable again at its own rate
    assert _put(http_client, t, t.product_id, {"is_tax_exempt": False}).status_code == 200
    pay = "18" if inclusive else "21.06"
    r = sale(http_client, t, qty="3", price="6", pay=pay)
    assert r.status_code == 201, r.text
    assert D(r.json()["lines"][0]["tax_rate"]) == D("17")
    assert D(r.json()["total_tax"]) == (D("2.62") if inclusive else D("3.06"))


# ── sync feed ────────────────────────────────────────────────────────────────


def test_sync_feed_carries_product_tax_fields(http_client, pos_tenant):
    t = pos_tenant
    rid = make_rate(http_client, t, "17")
    p = _create(http_client, t, tax_rate_id=rid, is_tax_exempt=True)
    changes, _, _ = snapshot_all(http_client, t)
    row = next(c for c in changes if c["entity"] == "product" and c["id"] == p["id"])
    assert row["data"]["tax_rate_id"] == rid and row["data"]["is_tax_exempt"] is True
    # and a later edit is stamped so devices pull it
    _put(http_client, t, p["id"], {"tax_rate_id": None, "is_tax_exempt": False})
    changes, _, _ = snapshot_all(http_client, t)
    row = next(c for c in changes if c["entity"] == "product" and c["id"] == p["id"])
    assert row["data"]["tax_rate_id"] is None and row["data"]["is_tax_exempt"] is False


# ── CSV import / export ──────────────────────────────────────────────────────

HEADER = "name,sku,unit,price,tax_rate_name,tax_exempt\n"


def _import(client, t, text, *, dry_run=False):
    return client.post(t.url("/products/import"), headers=t.owner_headers, json={"csv": text, "dry_run": dry_run})


def _unit_symbol(t) -> str:
    return db_rows("SELECT symbol FROM units WHERE id = :i", {"i": t.unit_id})[0]["symbol"]


def test_import_sets_rate_and_exempt_and_is_idempotent(http_client, pos_tenant):
    t = pos_tenant
    make_rate(http_client, t, "17", "Standard GST")
    u = _unit_symbol(t)
    body = HEADER + f"Soap,IMP-1,{u},60,standard gst,\nBread,IMP-2,{u},30,,yes\n"
    r = _import(http_client, t, body)
    assert r.status_code == 200, r.text
    rows = db_rows(
        "SELECT sku, tax_rate_id, is_tax_exempt FROM products WHERE business_id = :b AND sku LIKE 'IMP-%' ORDER BY sku",
        {"b": t.business_id},
    )
    assert rows[0]["tax_rate_id"] is not None and rows[0]["is_tax_exempt"] is False
    assert rows[1]["tax_rate_id"] is None and rows[1]["is_tax_exempt"] is True
    again = _import(http_client, t, body).json()
    assert again["summary"]["unchanged"] == 2

    # update through import: flip Bread to taxable, audited
    upd = _import(http_client, t, HEADER + f"Bread,IMP-2,{u},30,Standard GST,false\n").json()
    assert upd["summary"]["update"] == 1
    bread = db_rows("SELECT id, tax_rate_id, is_tax_exempt FROM products WHERE sku = 'IMP-2' AND business_id = :b",
                    {"b": t.business_id})[0]
    assert bread["tax_rate_id"] is not None and bread["is_tax_exempt"] is False
    audit = db_rows(
        "SELECT new_values FROM audit_logs WHERE record_id = :p AND table_name = 'products' "
        "AND new_values->>'source' = 'import'", {"p": bread["id"]},
    )
    assert len(audit) == 1


def test_import_unknown_rate_is_a_row_error(http_client, pos_tenant):
    t = pos_tenant
    u = _unit_symbol(t)
    r = _import(http_client, t, HEADER + f"Ghost,GH-1,{u},5,No Such Rate,\n", dry_run=True)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["action"] == "error" and any("unknown tax rate" in e for e in row["errors"])
    bad = _import(http_client, t, HEADER + f"Odd,OD-1,{u},5,,maybe\n", dry_run=True).json()["rows"][0]
    assert bad["action"] == "error"
    assert db_rows("SELECT 1 FROM products WHERE business_id = :b AND sku = 'GH-1'", {"b": t.business_id}) == []


def test_export_import_round_trip(http_client, pos_tenant, api_base, unique_suffix):
    a = pos_tenant
    rid = make_rate(http_client, a, "17", "GST17")
    u = _unit_symbol(a)
    _create(http_client, a, sku="RT-1", tax_rate_id=rid)
    _create(http_client, a, sku="RT-2", is_tax_exempt=True)
    _create(http_client, a, sku="RT-3")

    exp = http_client.get(a.url("/export/products"), headers=a.owner_headers, params={"format": "csv"})
    assert exp.status_code == 200, exp.text
    rows = list(csv.DictReader(io.StringIO(exp.content.decode("utf-8-sig"))))
    key = lambda name: next(k for k in rows[0] if k.strip().lower().replace(" ", "_") == name)  # noqa: E731
    by_sku = {r[key("sku")]: r for r in rows if r[key("sku")]}
    assert by_sku["RT-1"][key("tax_rate_name")] == "GST17" and by_sku["RT-1"][key("tax_exempt")] == "false"
    assert by_sku["RT-2"][key("tax_exempt")] == "true" and by_sku["RT-2"][key("tax_rate_name")] == ""

    # import the exported file into a second business that has a rate of the same name
    b = build_pos_tenant(http_client, api_base, unique_suffix + 7, phone_prefix="442")
    make_rate(http_client, b, "17", "GST17")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "sku", "unit", "tax_rate_name", "tax_exempt"])
    for sku, r in by_sku.items():
        w.writerow([r[key("name")], sku, _unit_symbol(b), r[key("tax_rate_name")], r[key("tax_exempt")]])
    imp = _import(http_client, b, out.getvalue())
    assert imp.status_code == 200, imp.text
    got = {
        r["sku"]: (r["rate_name"], r["is_tax_exempt"])
        for r in db_rows(
            "SELECT p.sku, tr.name AS rate_name, p.is_tax_exempt FROM products p "
            "LEFT JOIN tax_rates tr ON tr.id = p.tax_rate_id WHERE p.business_id = :b AND p.sku LIKE 'RT-%'",
            {"b": b.business_id},
        )
    }
    assert got == {"RT-1": ("GST17", False), "RT-2": (None, True), "RT-3": (None, False)}
    # and re-importing is a no-op
    assert _import(http_client, b, out.getvalue()).json()["summary"]["unchanged"] == 3
    assert u  # unit symbol of tenant A was readable
