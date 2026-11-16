"""Shared builders for the tax / price settings integration tests."""

from __future__ import annotations

from typing import Any

from tests.helpers.db import db_execute
from tests.helpers.phase2 import D, post_sale


def put_setting(client, t, key: str, value: Any, headers: dict | None = None):
    return client.put(
        t.url("/settings"),
        headers=headers or t.owner_headers,
        json={"setting_key": key, "setting_value": {"value": value}},
    )


def configure(client, t, settings: dict[str, Any]) -> None:
    for key, value in settings.items():
        r = put_setting(client, t, key, value)
        assert r.status_code == 200, r.text


def make_rate(client, t, rate: str = "17", name: str = "GST") -> str:
    r = client.post(t.url("/tax-rates"), headers=t.owner_headers, json={"name": name, "rate": rate})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def use_rate_on_product(t, rate_id: str, product_id: str | None = None) -> None:
    db_execute(
        "UPDATE products SET tax_rate_id = :r WHERE id = :p",
        {"r": rate_id, "p": product_id or t.product_id},
    )


def sale(client, t, *, qty="1", price="6", pay=None, **kw):
    """POST /sales paying ``pay`` in cash (default qty*price; callers pass the taxed total)."""
    pay = pay if pay is not None else str(D(qty) * D(price))
    return post_sale(
        client, t, qty=qty, unit_price=price,
        payments=[{"payment_method": "cash", "amount": pay}], **kw,
    )
