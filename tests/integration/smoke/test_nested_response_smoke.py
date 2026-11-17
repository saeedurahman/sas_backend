"""API smoke: every resource whose response schema nests ORM relationships.

Why: models load relationships with ``lazy="selectin"`` / ``"noload"`` and the async session
cannot lazy-load, so a response schema that reads a relationship the service did not
eager-load fails *during serialization* with ``MissingGreenlet`` (a bare HTTP 500). These tests
create / read / update / list each main resource through the real API with deeply nested data
and assert 2xx plus the nested payload. The autouse ``no_missing_greenlet`` fixture (conftest)
additionally fails a test if the server log shows MissingGreenlet, and ``managed_server`` fails
the session at teardown for the same reason.
"""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.phase2 import second_branch, stock_in
from tests.helpers.sync import by_entity, pull_all, snapshot_all
from tests.helpers.tenants import TenantContext, build_pos_tenant

pytestmark = pytest.mark.integration


def ok(resp: httpx.Response, label: str, expected: int | tuple[int, ...] = (200, 201)) -> dict | list:
    """Assert a 2xx (explicitly not 5xx) and return the JSON body."""
    assert resp.status_code < 500, f"{label}: HTTP {resp.status_code} (server error) {resp.text[:400]}"
    codes = (expected,) if isinstance(expected, int) else expected
    assert resp.status_code in codes, f"{label}: HTTP {resp.status_code} {resp.text[:400]}"
    return resp.json() if resp.content else {}


class World:
    """One tenant with a rich, nested data set shared by every test in this module."""

    def __init__(self, client: httpx.Client, t: TenantContext) -> None:
        self.c = client
        self.t = t
        self.h = t.owner_headers
        self.ids: dict[str, str] = {}

    # tiny request helpers -------------------------------------------------
    def get(self, path: str, label: str = "", **kw):
        return ok(self.c.get(self.t.url(path), headers=self.h, **kw), label or f"GET {path}")

    def post(self, path: str, body: dict | None = None, label: str = "", expected=(200, 201)):
        return ok(
            self.c.post(self.t.url(path), headers=self.h, json=body or {}),
            label or f"POST {path}",
            expected,
        )

    def put(self, path: str, body: dict, label: str = ""):
        return ok(self.c.put(self.t.url(path), headers=self.h, json=body), label or f"PUT {path}")


@pytest.fixture(scope="module")
def world(api_base: str, require_server) -> World:
    with httpx.Client(timeout=60.0) as client:
        suffix = uuid.uuid4().int % 10_000_000
        t = build_pos_tenant(client, api_base, suffix, business_name="Smoke", phone_prefix="480")
        yield World(client, t)


# ── catalog: products / categories / variations / barcodes ───────────────────


@pytest.fixture(scope="module")
def catalog(world: World) -> dict:
    """3-level category tree + brand + 2nd unit + tax rate, and one fully nested product."""
    w = world
    root = w.post("/categories", {"name": "Drinks"}, "category root")
    mid = w.post("/categories", {"name": "Hot drinks", "parent_id": root["id"]}, "category mid")
    leaf = w.post("/categories", {"name": "Coffee", "parent_id": mid["id"]}, "category leaf")
    brand = w.post("/brands", {"name": "Acme"}, "brand")
    unit2 = w.post("/units", {"name": "Box", "symbol": "bx"}, "unit")
    tax = w.post("/tax-rates", {"name": "GST 17", "rate": "17"}, "tax rate")
    product = w.post(
        "/products",
        {
            "name": "Smoke Coffee",
            "category_id": mid["id"],  # has a child (leaf) and a parent (root): the 500 case
            "brand_id": brand["id"],
            "base_unit_id": w.t.unit_id,
            "product_type": "standard",
            "tracking_type": "none",
            "tax_rate_id": tax["id"],
            "is_tax_exempt": False,
            "track_expiry": True,
            "name_secondary": "کافی",
            "barcodes": [{"barcode": "SMK-PRODUCT-0001"}],
            "variations": [
                {
                    "name": "250g",
                    "sku": f"SMK-250-{w.t.suffix}",
                    "unit_id": unit2["id"],
                    "is_default": True,
                    "weight_grams": "250",
                    "barcodes": [
                        {"barcode": "SMK-250-0001", "is_primary": True},
                        {"barcode": "SMK-250-0002", "barcode_type": "CODE128"},
                    ],
                },
                {"name": "500g", "sku": f"SMK-500-{w.t.suffix}", "barcodes": [{"barcode": "SMK-500-0001"}]},
            ],
        },
        "product create (variations + barcodes + category/brand/unit/tax)",
        expected=201,
    )
    return {"root": root, "mid": mid, "leaf": leaf, "brand": brand, "unit2": unit2, "tax": tax, "product": product}


def _assert_full_product(p: dict, cat: dict) -> None:
    assert p["category"] == {
        "id": cat["mid"]["id"],
        "name": "Hot drinks",
        "parent_id": cat["root"]["id"],
    }, "category must be the slim brief (no children)"
    assert "children" not in p["category"]
    assert p["brand"]["id"] == cat["brand"]["id"]
    assert p["base_unit"]["symbol"] == "pc"
    assert p["tax_rate_id"] == cat["tax"]["id"] and float(p["tax_rate"]) == 17.0
    assert p["is_tax_exempt"] is False and p["track_expiry"] is True
    variations = {v["name"]: v for v in p["variations"]}
    assert set(variations) == {"250g", "500g"}
    assert variations["250g"]["unit"]["symbol"] == "bx"
    assert {b["barcode"] for b in variations["250g"]["barcodes"]} == {"SMK-250-0001", "SMK-250-0002"}
    assert {b["barcode"] for b in variations["500g"]["barcodes"]} == {"SMK-500-0001"}
    assert {b["barcode"] for b in p["barcodes"]} == {
        "SMK-PRODUCT-0001", "SMK-250-0001", "SMK-250-0002", "SMK-500-0001",
    }


def test_product_create_returns_full_product(world: World, catalog: dict):
    _assert_full_product(catalog["product"], catalog)


def test_product_create_with_category_without_children_and_parent_levels(world: World, catalog: dict):
    for key in ("root", "mid", "leaf"):  # root has a 2-level subtree, leaf has none
        cat = catalog[key]
        p = world.post(
            "/products",
            {"name": f"On {key}", "category_id": cat["id"], "base_unit_id": world.t.unit_id,
             "product_type": "standard", "tracking_type": "none"},
            f"product on {key} category",
            expected=201,
        )
        assert p["category"]["id"] == cat["id"] and "children" not in p["category"]
        assert len(p["variations"]) == 1 and p["variations"][0]["is_default"] is True


def test_product_get_list_barcode_update(world: World, catalog: dict):
    w, pid = world, catalog["product"]["id"]
    _assert_full_product(w.get(f"/products/{pid}"), catalog)
    _assert_full_product(w.get("/products/barcode/SMK-250-0002"), catalog)

    page = w.get("/products", params={"category_id": catalog["mid"]["id"], "limit": 50})
    assert page["total"] >= 1 and any(i["id"] == pid for i in page["items"])
    assert all("category" not in i for i in page["items"])  # list rows are flat
    w.get("/products", params={"search": "Smoke", "brand_id": catalog["brand"]["id"]})

    # update: move to the leaf category -> response must reflect the NEW category
    updated = w.put(
        f"/products/{pid}",
        {"category_id": catalog["leaf"]["id"], "description": "d", "tax_rate_id": None, "is_tax_exempt": True},
        "product update",
    )
    assert updated["category"]["id"] == catalog["leaf"]["id"]
    assert updated["category"]["parent_id"] == catalog["mid"]["id"]
    assert updated["tax_rate_id"] is None and updated["tax_rate"] is None and updated["is_tax_exempt"] is True
    assert len(updated["variations"]) == 2 and len(updated["barcodes"]) == 4
    # restore for the other tests
    w.put(f"/products/{pid}", {"category_id": catalog["mid"]["id"], "tax_rate_id": catalog["tax"]["id"],
                               "is_tax_exempt": False}, "product restore")


def test_variation_and_barcode_endpoints(world: World, catalog: dict):
    w, pid = world, catalog["product"]["id"]
    var = w.post(
        f"/products/{pid}/variations",
        {"name": "1kg", "unit_id": catalog["unit2"]["id"], "barcodes": [{"barcode": "SMK-1KG-0001"}]},
        "variation create",
        expected=201,
    )
    assert var["unit"]["id"] == catalog["unit2"]["id"]
    assert [b["barcode"] for b in var["barcodes"]] == ["SMK-1KG-0001"]

    upd = w.put(f"/products/{pid}/variations/{var['id']}", {"name": "1kg pack"}, "variation update")
    assert upd["name"] == "1kg pack"
    # regression: db.refresh() used to reset barcodes/unit to empty on update
    assert upd["unit"]["id"] == catalog["unit2"]["id"]
    assert [b["barcode"] for b in upd["barcodes"]] == ["SMK-1KG-0001"]

    bc = w.post(f"/products/{pid}/barcodes", {"barcode": "SMK-EXTRA-0001", "variation_id": var["id"]},
                "barcode create", expected=201)
    assert bc["variation_id"] == var["id"]
    assert len(w.get(f"/products/{pid}")["variations"]) == 3


def test_product_create_is_atomic(world: World, catalog: dict):
    w = world
    before = w.get("/products", params={"search": "Atomic", "limit": 200})["total"]
    # 2nd variation's barcode already exists (belongs to the catalog product) -> whole request fails
    r = w.c.post(
        w.t.url("/products"), headers=w.h,
        json={"name": "Atomic fail", "base_unit_id": w.t.unit_id, "product_type": "standard",
              "tracking_type": "none", "sku": f"ATOMIC-{w.t.suffix}",
              "variations": [{"name": "A", "barcodes": [{"barcode": "ATOMIC-NEW-001"}]},
                             {"name": "B", "barcodes": [{"barcode": "SMK-500-0001"}]}]},
    )
    assert r.status_code == 409, r.text
    assert w.get("/products", params={"search": "Atomic", "limit": 200})["total"] == before
    # nothing leaked: the first variation's barcode and the SKU are free again
    again = w.post(
        "/products",
        {"name": "Atomic ok", "base_unit_id": w.t.unit_id, "product_type": "standard", "tracking_type": "none",
         "sku": f"ATOMIC-{w.t.suffix}", "variations": [{"name": "A", "barcodes": [{"barcode": "ATOMIC-NEW-001"}]}]},
        "retry after failed create",
        expected=201,
    )
    assert [b["barcode"] for b in again["barcodes"]] == ["ATOMIC-NEW-001"]

    for body, label in (
        ({"barcodes": [{"barcode": "DUP-0001"}, {"barcode": "DUP-0001"}]}, "duplicate in request"),
        ({"barcodes": [{"barcode": "PRI-0001", "is_primary": True}, {"barcode": "PRI-0002", "is_primary": True}]},
         "two primaries"),
    ):
        r = w.c.post(
            w.t.url("/products"), headers=w.h,
            json={"name": f"Atomic {label}", "base_unit_id": w.t.unit_id, "product_type": "standard",
                  "tracking_type": "none", **body},
        )
        assert r.status_code == 400, f"{label}: {r.status_code} {r.text}"
    assert w.get("/products", params={"search": "Atomic ", "limit": 200})["total"] == before + 1


def test_categories_tree_endpoints(world: World, catalog: dict):
    w = world
    roots = w.get("/categories")
    root = next(c for c in roots if c["id"] == catalog["root"]["id"])
    assert root["children"][0]["id"] == catalog["mid"]["id"]
    assert root["children"][0]["children"][0]["id"] == catalog["leaf"]["id"]  # 3 levels deep
    w.get("/categories", params={"parent_id": catalog["root"]["id"]})
    detail = w.get(f"/categories/{catalog['root']['id']}")
    assert detail["children"][0]["children"][0]["name"] == "Coffee"
    upd = w.put(f"/categories/{catalog['mid']['id']}", {"sort_order": 3}, "category update")
    assert upd["children"][0]["id"] == catalog["leaf"]["id"]
    # a 4th level created later is served too
    deeper = w.post("/categories", {"name": "Espresso", "parent_id": catalog["leaf"]["id"]}, "category L4")
    assert deeper["parent_id"] == catalog["leaf"]["id"]
    detail = w.get(f"/categories/{catalog['root']['id']}")
    assert detail["children"][0]["children"][0]["children"][0]["name"] == "Espresso"


def test_expense_categories_tree_and_expenses(world: World):
    w = world
    top = w.post("/expenses/categories", {"name": "Operations"}, "expense category", expected=201)
    mid = w.post("/expenses/categories", {"name": "Utilities", "parent_id": top["id"]}, "expense category 2", expected=201)
    w.post("/expenses/categories", {"name": "Electric", "parent_id": mid["id"]}, "expense category 3", expected=201)
    tree = w.get("/expenses/categories")
    assert any(c["id"] == top["id"] for c in tree)
    w.get(f"/expenses/categories/{top['id']}")
    w.put(f"/expenses/categories/{mid['id']}", {"name": "Utilities+"}, "expense category update")

    exp = w.post(
        "/expenses",
        {"branch_id": w.t.branch_id, "expense_category_id": mid["id"], "expense_date": date.today().isoformat(),
         "amount": "40.00", "payments": [{"payment_method": "cash", "amount": "25.00"},
                                          {"payment_method": "cash", "amount": "15.00"}]},
        "expense create",
        expected=201,
    )
    assert len(exp["payments"]) == 2
    assert len(w.get(f"/expenses/{exp['id']}")["payments"]) == 2
    w.put(f"/expenses/{exp['id']}", {"description": "edited"}, "expense update")
    w.get("/expenses")


# ── people, prices ───────────────────────────────────────────────────────────


def test_customers_suppliers_price_lists(world: World, catalog: dict):
    w = world
    pl = w.post("/prices/lists", {"name": "Wholesale", "list_type": "wholesale"}, "price list create", expected=201)
    w.post(f"/prices/lists/{pl['id']}/items",
           {"product_id": catalog["product"]["id"], "unit_price": "90.00"}, "price item", expected=(200, 201))
    w.get(f"/prices/lists/{pl['id']}")
    w.get("/prices/lists")
    w.get(f"/prices/lists/{pl['id']}/items")
    w.put(f"/prices/lists/{pl['id']}", {"name": "Wholesale 2"}, "price list update")
    w.post(f"/prices/lists/{pl['id']}/copy", {"name": "Wholesale copy"}, "price list copy", expected=(200, 201))
    w.get(f"/prices/product/{catalog['product']['id']}")

    cust = w.post("/customers", {"name": "Smoke Customer", "credit_limit": "500", "price_list_id": pl["id"]},
                  "customer create", expected=201)
    w.get(f"/customers/{cust['id']}")
    w.get("/customers")
    w.put(f"/customers/{cust['id']}", {"name": "Smoke Customer 2"}, "customer update")
    sup = w.post("/suppliers", {"name": "Smoke Supplier"}, "supplier create", expected=201)
    w.get(f"/suppliers/{sup['id']}")
    w.get("/suppliers")
    w.put(f"/suppliers/{sup['id']}", {"name": "Smoke Supplier 2"}, "supplier update")
    w.ids.update(customer_id=cust["id"], supplier_id=sup["id"])


# ── sales / returns / invoice ────────────────────────────────────────────────


def test_sale_return_invoice_flow(world: World, catalog: dict):
    w, t = world, world.t
    stock_in(w.c, t, catalog["product"]["id"], "50")
    cust = w.post("/customers", {"name": "Sale Customer", "credit_limit": "10000"}, "customer", expected=201)

    sale = w.post(
        "/sales",
        {
            "branch_id": t.branch_id,
            "customer_id": cust["id"],
            "lines": [
                {"product_id": t.product_id, "qty": "2", "unit_price": "100.00"},
                {"product_id": catalog["product"]["id"], "qty": "1", "unit_price": "250.00"},
            ],
            "payments": [{"payment_method": "cash", "amount": "300.00"},
                         {"payment_method": "card", "amount": "150.00"}],
        },
        "sale create",
        expected=201,
    )
    assert len(sale["lines"]) == 2 and len(sale["payments"]) == 2
    sid = sale["id"]
    got = w.get(f"/sales/{sid}")
    assert len(got["lines"]) == 2 and len(got["payments"]) == 2
    w.get("/sales")
    w.get("/sales", params={"customer_id": cust["id"]})
    w.get(f"/invoice/{sid}")
    w.get(f"/invoice/{sid}/thermal")

    ret = w.post(
        "/returns",
        {"branch_id": t.branch_id, "sale_id": sid, "customer_id": cust["id"],
         "lines": [{"sale_line_id": sale["lines"][0]["id"], "qty": "1"}],
         "refund_payments": [{"payment_method": "cash", "amount": "100.00"}]},
        "return create",
        expected=201,
    )
    assert len(ret["lines"]) == 1 and len(ret["payments"]) == 1
    assert len(w.get(f"/returns/{ret['id']}")["lines"]) == 1
    w.get("/returns")

    # void a completed sale (full SaleResponse again)
    void_sale = w.post(
        "/sales",
        {"branch_id": t.branch_id, "lines": [{"product_id": t.product_id, "qty": "1", "unit_price": "100.00"}],
         "payments": [{"payment_method": "cash", "amount": "100.00"}]},
        "sale to void", expected=201,
    )
    voided = w.post(f"/sales/{void_sale['id']}/void", None, "sale void")
    assert len(voided["lines"]) == 1 and len(voided["payments"]) >= 1
    w.post("/sales/price-preview",
           {"branch_id": t.branch_id, "customer_id": cust["id"],
            "lines": [{"product_id": t.product_id, "qty": "3", "unit_price": "100.00"}]}, "price preview")
    w.ids["sale_id"] = sid


# ── purchasing / stock documents ─────────────────────────────────────────────


def test_purchase_order_receipt_and_stock_documents(world: World, catalog: dict):
    w, t = world, world.t
    sup = w.post("/suppliers", {"name": "PO Supplier"}, "supplier", expected=201)
    po = w.post(
        "/purchases/orders",
        {"supplier_id": sup["id"], "branch_id": t.branch_id,
         "lines": [{"product_id": t.product_id, "ordered_qty": "10", "cost_per_unit": "40.00"},
                   {"product_id": catalog["product"]["id"], "ordered_qty": "5", "cost_per_unit": "60.00"}]},
        "purchase order create",
        expected=201,
    )
    assert po["supplier"]["id"] == sup["id"] and len(po["lines"]) == 2
    got = w.get(f"/purchases/orders/{po['id']}")
    assert got["supplier"]["id"] == sup["id"] and len(got["lines"]) == 2
    w.get("/purchases/orders")
    w.put(f"/purchases/orders/{po['id']}/status", {"status": "ordered"}, "purchase order status")

    grn = w.post(
        "/purchases/receipts",
        {"branch_id": t.branch_id, "supplier_id": sup["id"], "purchase_order_id": po["id"],
         "lines": [{"product_id": t.product_id, "purchase_line_id": po["lines"][0]["id"],
                    "qty_received": "10", "cost_per_unit": "40.00"}]},
        "goods receipt create",
        expected=201,
    )
    assert grn["supplier"]["id"] == sup["id"] and len(grn["lines"]) == 1
    assert len(w.get(f"/purchases/receipts/{grn['id']}")["lines"]) == 1
    w.get(f"/supplier-ledger/{sup['id']}")

    # adjustments / waste / transfers carry nested lines too
    w.post("/adjustments", {"branch_id": t.branch_id, "reason": "count_correction",
                            "lines": [{"product_id": t.product_id, "qty_delta": "-1", "cost_per_unit": "40"}]},
           "adjustment", expected=201)
    adj = w.get("/adjustments")
    assert adj, "adjustments list is empty"
    w.get(f"/adjustments/{adj[0]['id']}")
    waste = w.post("/waste", {"branch_id": t.branch_id, "reason": "expiry",
                              "lines": [{"product_id": t.product_id, "qty": "1", "cost_per_unit": "40"}]},
                   "waste", expected=201)
    w.get(f"/waste/{waste['id']}")
    w.get("/waste")
    dest = second_branch(w.c, t)
    tr = w.post("/transfers", {"source_branch_id": t.branch_id, "dest_branch_id": dest,
                               "lines": [{"product_id": t.product_id, "qty": "2"}]}, "transfer", expected=201)
    assert len(tr["lines"]) == 1
    w.get(f"/transfers/{tr['id']}")
    w.get("/transfers")
    assert len(w.post(f"/transfers/{tr['id']}/receive", None, "transfer receive")["lines"]) == 1
    w.get("/stock/movements")


# ── restaurant, manufacturing, accounting, receipts, sync ────────────────────


def test_restaurant_modifiers_tables_and_kot(world: World, catalog: dict):
    w, t = world, world.t
    w.put("/business/config",
          {"config_json": {"enable_restaurant": True, "enable_table_management": True, "enable_kot": True}}, "enable restaurant")
    g = w.post("/restaurant/modifier-groups",
               {"name": "Size", "selection_type": "single", "is_required": True, "max_selections": 1},
               "modifier group", expected=201)
    modifier = w.post(f"/restaurant/modifier-groups/{g['id']}/modifiers", {"name": "Large", "price_delta": "20"},
                      "modifier create", expected=(200, 201))
    assert w.get(f"/restaurant/modifier-groups/{g['id']}")["modifiers"]
    w.get("/restaurant/modifier-groups")
    linked = w.put(f"/restaurant/products/{t.product_id}/modifier-groups", {"modifier_group_ids": [g["id"]]},
                   "link modifier groups")
    assert linked and linked[0]["modifiers"]
    w.get(f"/restaurant/products/{t.product_id}/modifier-groups")

    plan = w.post("/restaurant/floor-plans", {"branch_id": t.branch_id, "name": "Hall"}, "floor plan",
                  expected=(200, 201))
    table = w.post("/restaurant/tables",
                   {"branch_id": t.branch_id, "floor_plan_id": plan["id"], "table_number": "T1"},
                   "table", expected=(200, 201))
    w.post("/restaurant/tables", {"branch_id": t.branch_id, "table_number": "T2"}, "unassigned table",
           expected=(200, 201))
    layout = w.get("/restaurant/floor-layout", params={"branch_id": t.branch_id})
    assert layout["floor_plans"] and layout["unassigned_tables"]
    w.get("/restaurant/kot/active", params={"branch_id": t.branch_id})

    # dine-in tab: open -> add line with modifier -> KOT -> bill -> complete (SaleResponse nests lines/payments)
    tab = w.post("/sales/open-tab", {"branch_id": t.branch_id, "table_id": table["id"]}, "open tab", expected=201)
    stock_in(w.c, t, t.product_id, "20")
    tab = w.post(f"/sales/{tab['id']}/lines",
                 {"lines": [{"product_id": t.product_id, "qty": "2", "unit_price": "100.00",
                             "modifier_ids": [modifier["id"]]}]}, "tab add lines")
    assert len(tab["lines"]) == 1
    w.get(f"/sales/{tab['id']}")
    w.get(f"/restaurant/kot/by-table/{table['id']}")
    w.get("/restaurant/kot/active", params={"branch_id": t.branch_id})
    w.post(f"/sales/{tab['id']}/request-bill", None, "request bill")
    done = w.post(f"/sales/{tab['id']}/complete",
                  {"payments": [{"payment_method": "cash", "amount": "240.00"}]}, "complete tab")
    assert len(done["lines"]) == 1 and len(done["payments"]) == 1


def test_manufacturing_bom_and_production_order(world: World, catalog: dict):
    w, t = world, world.t
    w.put("/business/config", {"config_json": {"enable_manufacturing": True}}, "enable manufacturing")
    finished = w.post("/products", {"name": "Smoke Bread", "base_unit_id": t.unit_id,
                                    "product_type": "manufactured", "tracking_type": "none"},
                      "manufactured product", expected=201)
    bom = w.post("/manufacturing/boms",
                 {"product_id": finished["id"], "name": "Bread BOM", "output_qty": "10", "is_active": True,
                  "lines": [{"ingredient_product_id": t.product_id, "qty_required": "2"},
                            {"ingredient_product_id": catalog["product"]["id"], "qty_required": "1"}]},
                 "bom create", expected=201)
    assert len(bom["lines"]) == 2
    assert len(w.get(f"/manufacturing/boms/{bom['id']}")["lines"]) == 2
    w.get("/manufacturing/boms")
    w.get(f"/manufacturing/boms/by-product/{finished['id']}")
    order = w.post("/manufacturing/production-orders",
                   {"branch_id": t.branch_id, "bom_header_id": bom["id"], "qty_to_produce": "10"},
                   "production order create", expected=201)
    assert order["bom"]["id"] == bom["id"]
    w.get(f"/manufacturing/production-orders/{order['id']}")
    w.get("/manufacturing/production-orders")
    started = w.post(f"/manufacturing/production-orders/{order['id']}/start", None, "production start")
    assert started["bom"]["id"] == bom["id"]
    assert w.get(f"/manufacturing/production-orders/{order['id']}")["bom"]["id"] == bom["id"]


def test_accounting_receipts_settings_and_sync(world: World, catalog: dict):
    w, t = world, world.t
    w.put("/business/config", {"config_json": {"enable_accounting": True}}, "enable accounting")
    tree = w.get("/accounting/coa")
    assert tree
    w.get("/accounting/journal-entries")
    layouts = w.get("/receipt-layouts")
    assert layouts
    w.get("/receipt-layouts/assignments")
    w.get("/receipt-layouts/designs")
    w.get("/business/me")

    changes, _cursor = pull_all(w.c, t)
    snap, _cursor, _pages = snapshot_all(w.c, t)
    for feed in (changes, snap):
        assert by_entity(feed, "product"), "product missing from sync feed"
        assert by_entity(feed, "category"), "category missing from sync feed"
    w.get("/sync/flags")


# ── generic sweep: every parameterless GET endpoint ──────────────────────────

# Platform-admin routes need a different credential; health/docs are not tenant APIs.
_SWEEP_SKIP = ("/platform/", "/license", "/health", "/docs", "/openapi", "/redoc")


def test_every_parameterless_get_endpoint_survives_nested_data(world: World, api_base: str):
    """Hit every GET list endpoint of the tenant API with a data-rich tenant; none may 5xx."""
    spec = httpx.get(api_base.replace("/api/v1", "") + "/openapi.json", timeout=30.0).json()
    paths = sorted(
        p for p, ops in spec["paths"].items()
        if "get" in ops and "{" not in p and not any(s in p for s in _SWEEP_SKIP)
    )
    assert len(paths) > 40, f"suspiciously few GET endpoints discovered: {len(paths)}"
    failures = []
    for path in paths:
        full = path if path.startswith("http") else api_base.replace("/api/v1", "") + path
        resp = world.c.get(full, headers=world.h)
        if resp.status_code >= 500:
            failures.append(f"GET {path} -> {resp.status_code} {resp.text[:200]}")
    assert not failures, "5xx on GET endpoints:\n" + "\n".join(failures)
