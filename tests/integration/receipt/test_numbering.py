"""Register short codes and the official sale-number scheme."""

from __future__ import annotations

import re

import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_rows
from tests.helpers.phase2 import post_sale
from tests.helpers.sync import pull_json
from tests.integration.tax.helpers import put_setting

pytestmark = pytest.mark.integration


def _devices(c, t) -> dict[str, dict]:
    r = c.get(t.url("/devices"), headers=t.owner_headers)
    assert_status(r, 200, label="devices")
    return {d["device_id"]: d for d in r.json()}


# -- short codes --------------------------------------------------------------


def test_short_codes_assigned_in_registration_order_and_returned_on_pull(http_client, pos_tenant):
    t = pos_tenant
    a = pull_json(http_client, t, device_id="dev-a")
    b = pull_json(http_client, t, device_id="dev-b")
    again = pull_json(http_client, t, device_id="dev-a")
    assert (a["device_short_code"], b["device_short_code"]) == ("C1", "C2")
    assert again["device_short_code"] == "C1"
    assert a["number_prefix"] == ""
    devices = _devices(http_client, t)
    assert (devices["dev-a"]["short_code"], devices["dev-b"]["short_code"]) == ("C1", "C2")


def test_short_code_returned_on_push_registration(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.post(
        t.url("/sync/push"), headers=t.owner_headers,
        json={"device_id": "dev-push", "batch_id": "b1", "ops": []},
    )
    assert_status(r, 200, label="push")
    assert r.json()["device_short_code"] == "C1"


def test_owner_edits_short_code_with_uniqueness(http_client, pos_tenant):
    t = pos_tenant
    pull_json(http_client, t, device_id="dev-a")
    pull_json(http_client, t, device_id="dev-b")
    ok = http_client.put(t.url("/devices/dev-a"), headers=t.owner_headers, json={"short_code": "sh1"})
    assert_status(ok, 200, label="edit")
    assert ok.json()["short_code"] == "SH1"
    assert pull_json(http_client, t, device_id="dev-a")["device_short_code"] == "SH1"

    clash = http_client.put(t.url("/devices/dev-b"), headers=t.owner_headers, json={"short_code": "Sh1"})
    assert clash.status_code == 409
    for bad in ("A", "TOOLONG", "a b", "x!"):
        r = http_client.put(t.url("/devices/dev-b"), headers=t.owner_headers, json={"short_code": bad})
        assert r.status_code == 422, bad
    # keeping its own code is fine
    assert http_client.put(t.url("/devices/dev-a"), headers=t.owner_headers,
                           json={"short_code": "SH1"}).status_code == 200
    assert http_client.put(t.url("/devices/dev-a"), headers=t.cashier_headers,
                           json={"short_code": "ZZ"}).status_code == 403


def test_number_prefix_setting_is_in_pull_and_validated(http_client, pos_tenant):
    t = pos_tenant
    assert put_setting(http_client, t, "receipt.number_prefix", "hq").status_code == 200
    assert pull_json(http_client, t)["number_prefix"] == "HQ"
    assert put_setting(http_client, t, "receipt.number_prefix", "bad prefix!").status_code == 422


def test_client_receipt_no_unique_per_business(http_client, pos_tenant):
    from tests.helpers.sync import op, push_ok, sale_payload

    t = pos_tenant
    first = op("sale.create", sale_payload(t, receipt="C1-0001"))
    assert push_ok(http_client, t, [first])[0]["status"] == "applied"
    second = op("sale.create", sale_payload(t, receipt="C1-0001"))
    res = push_ok(http_client, t, [second])[0]
    assert res["status"] != "applied"


# -- official scheme ----------------------------------------------------------


def _numbers(t) -> list[dict]:
    return db_rows(
        "SELECT sale_number, sale_seq FROM sales WHERE business_id = :b ORDER BY created_at, id",
        {"b": t.business_id},
    )


def test_default_format_is_unchanged(http_client, pos_tenant):
    t = pos_tenant
    r = post_sale(http_client, t)
    assert_status(r, 201, label="sale")
    assert re.fullmatch(r"INV-\d{8}-\d{4}", r.json()["sale_number"])
    assert _numbers(t)[0]["sale_seq"] is None


def test_prefix_digits_and_start_apply(http_client, pos_tenant):
    t = pos_tenant
    for key, value in (("sales.invoice_prefix", "bill"), ("sales.invoice_digits", 5),
                       ("sales.invoice_start", 100)):
        assert put_setting(http_client, t, key, value).status_code == 200
    numbers = []
    for _ in range(3):
        r = post_sale(http_client, t)
        assert_status(r, 201, label="sale")
        numbers.append(r.json()["sale_number"])
    assert numbers == ["BILL-00100", "BILL-00101", "BILL-00102"]
    assert [n["sale_seq"] for n in _numbers(t)] == [100, 101, 102]


def test_numbers_sort_numerically_past_the_digit_width(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "sales.invoice_digits", 4)
    put_setting(http_client, t, "sales.invoice_start", 9998)
    for _ in range(4):
        assert_status(post_sale(http_client, t), 201, label="sale")
    rows = _numbers(t)
    assert [r["sale_number"] for r in rows] == ["INV-9998", "INV-9999", "INV-10000", "INV-10001"]
    by_seq = [r["sale_number"] for r in sorted(rows, key=lambda r: r["sale_seq"])]
    assert by_seq == [r["sale_number"] for r in rows]
    assert sorted(r["sale_number"] for r in rows) != by_seq  # plain text order would be wrong


def test_existing_numbers_never_change(http_client, pos_tenant):
    t = pos_tenant
    first = post_sale(http_client, t).json()["sale_number"]
    put_setting(http_client, t, "sales.invoice_prefix", "NEW")
    put_setting(http_client, t, "sales.invoice_start", 500)
    second = post_sale(http_client, t).json()["sale_number"]
    assert second == "NEW-0500"
    assert [n["sale_number"] for n in _numbers(t)] == [first, second]


def test_offline_replay_uses_the_official_scheme(http_client, pos_tenant):
    from tests.helpers.sync import op, push_ok, sale_payload

    t = pos_tenant
    put_setting(http_client, t, "sales.invoice_prefix", "OFF")
    put_setting(http_client, t, "sales.invoice_start", 7)
    res = push_ok(http_client, t, [op("sale.create", sale_payload(t, receipt="C1-0001"))])[0]
    assert res["status"] == "applied" and res["official_number"] == "OFF-0007"


@pytest.mark.parametrize(
    "key, value",
    [("sales.invoice_prefix", ""), ("sales.invoice_prefix", "x" * 30), ("sales.invoice_digits", 0),
     ("sales.invoice_digits", 11), ("sales.invoice_digits", "4"), ("sales.invoice_start", 0),
     ("sales.invoice_start", -3), ("inventory.pick_policy", "lifo"), ("inventory.expired_sale", "maybe")],
)
def test_setting_validation(http_client, pos_tenant, key, value):
    assert put_setting(http_client, pos_tenant, key, value).status_code == 422


def test_cashier_cannot_change_numbering(http_client, pos_tenant):
    t = pos_tenant
    r = put_setting(http_client, t, "sales.invoice_prefix", "X", headers=t.cashier_headers)
    assert r.status_code == 403
