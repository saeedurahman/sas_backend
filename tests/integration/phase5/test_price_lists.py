"""Price lists: management rules, bulk upsert, import, customer pricing, sync."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.sales import create_sale_payload
from tests.helpers.sqlexec import sql
from tests.helpers.sync import by_entity, pull_all, snapshot_all
from tests.helpers.tenants import build_pos_tenant

pytestmark = pytest.mark.integration

NOBODY = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def t(http_client, api_base, unique_suffix):
    t = build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="475")
    r = http_client.put(t.url(f"/products/{t.product_id}"), headers=t.owner_headers, json={"sku": "PL-1"})
    assert_status(r, 200, label="set sku")
    return t


def _mk(c, t, name, **extra):
    r = c.post(t.url("/prices/lists"), headers=t.owner_headers,
               json={"name": name, "list_type": "wholesale", **extra})
    assert_status(r, 201, label=f"create {name}")
    return r.json()


def _bulk(c, t, list_id, price, product_id=None, min_qty="1"):
    return c.post(
        t.url(f"/prices/lists/{list_id}/items/bulk"), headers=t.owner_headers,
        json={"items": [{"product_id": product_id or t.product_id, "price": price, "min_qty": min_qty}]},
    )


def _default(c, t):
    return next(x for x in c.get(t.url("/prices/lists"), headers=t.owner_headers).json() if x["is_default"])


def test_crud_default_switch_and_delete_rules(http_client, t):
    c = http_client
    retail = _default(c, t)
    ws = _mk(c, t, "Wholesale")
    assert ws["is_default"] is False

    r = c.put(t.url(f"/prices/lists/{ws['id']}"), headers=t.owner_headers, json={"name": "Wholesale 2"})
    assert r.json()["name"] == "Wholesale 2"

    # the default list cannot be unset, deactivated or deleted
    for body in ({"is_default": False}, {"is_active": False}):
        r = c.put(t.url(f"/prices/lists/{retail['id']}"), headers=t.owner_headers, json=body)
        assert r.status_code in (400, 409, 422), (body, r.text)
    assert c.delete(t.url(f"/prices/lists/{retail['id']}"), headers=t.owner_headers).status_code in (400, 409, 422)

    # switching the default keeps exactly one
    r = c.put(t.url(f"/prices/lists/{ws['id']}"), headers=t.owner_headers, json={"is_default": True})
    assert_status(r, 200, label="switch default")
    defaults = [x for x in c.get(t.url("/prices/lists"), headers=t.owner_headers).json() if x["is_default"]]
    assert [x["id"] for x in defaults] == [ws["id"]]
    assert sql("SELECT count(*) FROM price_lists WHERE business_id=:b AND is_default AND deleted_at IS NULL",
               b=t.business_id)[0][0] == 1

    # soft delete a non-default list; its name becomes reusable
    r = c.delete(t.url(f"/prices/lists/{retail['id']}"), headers=t.owner_headers)
    assert r.status_code == 200 and r.json()["deleted"] is True
    _mk(c, t, "Retail")


def test_validity_window_validation(http_client, t):
    r = http_client.post(t.url("/prices/lists"), headers=t.owner_headers,
                         json={"name": "Bad", "list_type": "custom",
                               "valid_from": "2030-02-01T00:00:00Z", "valid_to": "2030-01-01T00:00:00Z"})
    assert r.status_code == 422


def test_bulk_upsert_paging_search_delete(http_client, t):
    c = http_client
    lst = _mk(c, t, "Bulk")
    r = _bulk(c, t, lst["id"], "90.00")
    assert_status(r, 200, label="bulk create")
    assert r.json() == {"created": 1, "updated": 0, "unchanged": 0, "total": 1}
    assert _bulk(c, t, lst["id"], "95.00").json()["updated"] == 1
    assert _bulk(c, t, lst["id"], "95.00").json()["unchanged"] == 1
    assert _bulk(c, t, lst["id"], "85.00", min_qty="10").json()["created"] == 1  # quantity tier

    base = t.url(f"/prices/lists/{lst['id']}/items")
    page = c.get(base, headers=t.owner_headers, params={"limit": 1, "skip": 0}).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    assert c.get(base, headers=t.owner_headers, params={"search": "PL-1"}).json()["total"] == 2
    assert c.get(base, headers=t.owner_headers, params={"search": "zzzz-none"}).json()["total"] == 0

    # duplicate rows in one request and unknown products are rejected
    dup = {"product_id": t.product_id, "price": "1", "min_qty": "1"}
    assert c.post(base + "/bulk", headers=t.owner_headers, json={"items": [dup, dup]}).status_code == 422
    assert _bulk(c, t, lst["id"], "1", product_id=NOBODY).status_code in (404, 422)

    removed = page["items"][0]
    assert c.delete(base + f"/{removed['id']}", headers=t.owner_headers).status_code == 204
    assert c.get(base, headers=t.owner_headers).json()["total"] == 1
    # a deleted item can be added again
    assert _bulk(c, t, lst["id"], "77.00", min_qty=removed["min_qty"]).status_code == 200
    assert c.get(base, headers=t.owner_headers).json()["total"] == 2


def test_manage_permission_required(http_client, t):
    lst = _mk(http_client, t, "Perm")
    r = http_client.post(t.url(f"/prices/lists/{lst['id']}/items/bulk"), headers=t.cashier_headers,
                         json={"items": [{"product_id": t.product_id, "price": "1"}]})
    assert r.status_code == 403


def test_copy_and_percent_adjust(http_client, t):
    c = http_client
    lst = _mk(c, t, "Source")
    _bulk(c, t, lst["id"], "100.00")
    r = c.post(t.url(f"/prices/lists/{lst['id']}/copy"), headers=t.owner_headers,
               json={"name": "Copy +10", "percent": "10"})
    assert r.status_code in (200, 201), r.text
    copy = r.json()
    items = c.get(t.url(f"/prices/lists/{copy['id']}/items"), headers=t.owner_headers).json()["items"]
    assert [D(i["unit_price"]) for i in items] == [D("110.00")]

    base = t.url(f"/prices/lists/{lst['id']}")
    dry = c.post(base + "/adjust", headers=t.owner_headers, json={"percent": "-50", "dry_run": True}).json()
    assert dry["dry_run"] is True and dry["updated"] == 1
    items = c.get(base + "/items", headers=t.owner_headers).json()["items"]
    assert D(items[0]["unit_price"]) == D("100.00")  # a dry run changes nothing
    real = c.post(base + "/adjust", headers=t.owner_headers, json={"percent": "-50", "round_to": "5"}).json()
    assert real["updated"] == 1
    items = c.get(base + "/items", headers=t.owner_headers).json()["items"]
    assert D(items[0]["unit_price"]) == D("50.00")


def test_csv_import_dry_run_then_apply(http_client, t):
    c = http_client
    lst = _mk(c, t, "Imported")
    url = t.url(f"/prices/lists/{lst['id']}/import")
    items_url = t.url(f"/prices/lists/{lst['id']}/items")
    body = "sku,price\nPL-1,120.50\nNOPE,5\n"
    r = c.post(url, headers=t.owner_headers, json={"csv": body, "dry_run": True})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["dry_run"] is True
    assert rep["summary"]["create"] == 1 and rep["summary"]["errors"] == 1
    assert rep["summary"]["applied"] == 0
    assert c.get(items_url, headers=t.owner_headers).json()["total"] == 0

    # default on_error=abort: applying with an error row is refused, nothing written
    r = c.post(url, headers=t.owner_headers, json={"csv": body, "dry_run": False})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "import_has_errors"
    assert c.get(items_url, headers=t.owner_headers).json()["total"] == 0

    r = c.post(url, headers=t.owner_headers, json={"csv": body, "dry_run": False, "on_error": "skip"})
    assert r.status_code == 200 and r.json()["summary"]["applied"] == 1
    items = c.get(items_url, headers=t.owner_headers).json()["items"]
    assert [D(i["unit_price"]) for i in items] == [D("120.50")]
    again = c.post(url, headers=t.owner_headers,
                   json={"csv": "sku,price\nPL-1,120.50\n", "dry_run": False}).json()
    assert again["summary"]["unchanged"] == 1


def _customer(c, t, **extra):
    r = c.post(t.url("/customers"), headers=t.owner_headers, json={"name": "Whole Seller", **extra})
    assert_status(r, 201, label="customer")
    return r.json()


def _price_of(c, t, customer_id=None, qty="1"):
    params = {"qty": qty}
    if customer_id:
        params["customer_id"] = customer_id
    r = c.get(t.url(f"/prices/product/{t.product_id}"), headers=t.owner_headers, params=params)
    return r.json()["unit_price"]


def test_customer_list_with_default_fallback(http_client, t):
    c = http_client
    _bulk(c, t, _default(c, t)["id"], "100.00")
    ws = _mk(c, t, "Wholesale")
    cust = _customer(c, t, price_list_id=ws["id"])
    assert cust["price_list_id"] == ws["id"]

    assert D(_price_of(c, t, cust["id"])) == D("100.00")  # list has no price -> default
    _bulk(c, t, ws["id"], "80.00")
    _bulk(c, t, ws["id"], "70.00", min_qty="5")
    assert D(_price_of(c, t, cust["id"])) == D("80.00")
    assert D(_price_of(c, t, cust["id"], qty="6")) == D("70.00")
    assert D(_price_of(c, t)) == D("100.00")  # no customer

    # expired / not-yet-valid list -> default
    sql("UPDATE price_lists SET valid_to = now() - interval '1 day' WHERE id = :i", i=ws["id"])
    assert D(_price_of(c, t, cust["id"])) == D("100.00")
    sql("UPDATE price_lists SET valid_to = NULL, valid_from = now() + interval '1 day' WHERE id = :i",
        i=ws["id"])
    assert D(_price_of(c, t, cust["id"])) == D("100.00")


def test_sale_prices_lines_from_customer_list(http_client, t):
    c = http_client
    _bulk(c, t, _default(c, t)["id"], "100.00")
    ws = _mk(c, t, "Wholesale")
    _bulk(c, t, ws["id"], "80.00")
    cust = _customer(c, t, price_list_id=ws["id"])
    body = create_sale_payload(branch_id=t.branch_id, product_id=t.product_id, qty="2",
                               register_shift_id=t.shift_id, customer_id=cust["id"],
                               payments=[{"payment_method": "cash", "amount": "160.00"}])
    del body["lines"][0]["unit_price"]
    r = c.post(t.url("/sales"), headers=t.owner_headers, json=body)
    assert_status(r, 201, label="sale without price")
    sale = r.json()
    assert D(str(sql("SELECT unit_price FROM sale_lines WHERE sale_id=:s", s=sale["id"])[0][0])) == D("80")

    # no price anywhere and no unit_price -> 422
    other = c.post(t.url("/products"), headers=t.owner_headers,
                   json={"name": "Unpriced", "base_unit_id": t.unit_id, "product_type": "standard",
                         "tracking_type": "none"}).json()
    body["lines"][0]["product_id"] = other["id"]
    assert c.post(t.url("/sales"), headers=t.owner_headers, json=body).status_code == 422


def test_customer_assignment_validation_and_delete_unassigns(http_client, t):
    c = http_client
    ws = _mk(c, t, "Assigned")
    cust = _customer(c, t)
    url = t.url(f"/customers/{cust['id']}")
    r = c.put(url, headers=t.owner_headers, json={"price_list_id": ws["id"]})
    assert r.status_code == 200 and r.json()["price_list_id"] == ws["id"]
    assert c.put(url, headers=t.owner_headers, json={"price_list_id": NOBODY}).status_code == 422
    # an omitted field leaves the assignment alone
    assert c.put(url, headers=t.owner_headers, json={"name": "Renamed"}).json()["price_list_id"] == ws["id"]
    r = c.delete(t.url(f"/prices/lists/{ws['id']}"), headers=t.owner_headers)
    assert r.json()["customers_unassigned"] == 1
    assert c.get(url, headers=t.owner_headers).json()["price_list_id"] is None


def test_sync_carries_customer_list_and_validity(http_client, t):
    c = http_client
    ws = _mk(c, t, "Seasonal", valid_from="2030-01-01T00:00:00Z", valid_to="2030-12-31T00:00:00Z")
    cust = _customer(c, t, price_list_id=ws["id"])
    changes, cursor, _ = snapshot_all(c, t)
    mine = next(x for x in by_entity(changes, "customer") if x["id"] == cust["id"])
    assert mine["data"]["price_list_id"] == ws["id"]
    lst = next(x for x in by_entity(changes, "price_list") if x["id"] == ws["id"])
    assert lst["data"]["valid_from"].startswith("2030-01-01")
    assert lst["data"]["valid_to"].startswith("2030-12-31")

    # re-assigning the customer is a stamped change
    c.put(t.url(f"/customers/{cust['id']}"), headers=t.owner_headers, json={"price_list_id": None})
    later, cursor2 = pull_all(c, t, since=cursor)
    upd = [x for x in by_entity(later, "customer") if x["id"] == cust["id"]]
    assert upd and upd[-1]["data"]["price_list_id"] is None

    # list edits are stamped too
    c.put(t.url(f"/prices/lists/{ws['id']}"), headers=t.owner_headers, json={"name": "Seasonal 2"})
    later, _ = pull_all(c, t, since=cursor2)
    assert any(x["id"] == ws["id"] for x in by_entity(later, "price_list"))
