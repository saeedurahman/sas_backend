"""Builders for the Quick Add tests."""

from __future__ import annotations

import random
import uuid
from typing import Any

from app.services.barcode_utils import gtin_check_digit
from tests.helpers.db import db_rows, db_scalar
from tests.helpers.phase2 import D


def ean13(prefix: str = "896") -> str:
    """A fresh, valid EAN-13 (check digit computed)."""
    body = prefix + "".join(random.choice("0123456789") for _ in range(12 - len(prefix)))
    return body + str(gtin_check_digit(body))


def line(barcode: str | None = None, *, cid: str | None = None, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_line_id": cid or uuid.uuid4().hex[:12],
        "barcode": ean13() if barcode is None else barcode,
        "name": f"QA {uuid.uuid4().hex[:6]}",
        "qty": "10",
        "cost_price": "50.00",
        "sale_price": "70.00",
    }
    body.update(over)
    return body


def post_qa(client, t, lines, *, mode="opening_stock", headers=None, key=None, **extra):
    h = dict(headers or t.owner_headers)
    if key:
        h["Idempotency-Key"] = key
    extra.setdefault("branch_id", t.branch_id)  # pass branch_id=None to omit it
    return client.post(
        t.url("/products/quick-add"), headers=h, json={"mode": mode, "lines": lines, **extra}
    )


def quick_add(client, t, lines, **kw) -> dict:
    r = post_qa(client, t, lines, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def stock_of(t, product_id: str, branch_id: str | None = None):
    return D(
        db_scalar(
            "SELECT COALESCE(SUM(qty), 0) FROM stock_movements WHERE business_id = :b "
            "AND branch_id = :br AND product_id = :p AND deleted_at IS NULL",
            {"b": t.business_id, "br": branch_id or t.branch_id, "p": product_id},
        )
    )


def layers(t, product_id: str) -> list[dict]:
    return db_rows(
        "SELECT ordered_qty, received_qty, qty_remaining, cost_per_unit, batch_number, expiry_date, "
        "layer_source, purchase_order_id, branch_id FROM purchase_lines "
        "WHERE business_id = :b AND product_id = :p AND deleted_at IS NULL ORDER BY created_at",
        {"b": t.business_id, "p": product_id},
    )


def lookup(client, t, barcode: str, *, headers=None, **params):
    return client.get(
        t.url("/products/lookup"), headers=headers or t.owner_headers, params={"barcode": barcode, **params}
    )


def default_price(t, product_id: str):
    row = db_rows(
        "SELECT i.unit_price FROM price_list_items i JOIN price_lists l ON l.id = i.price_list_id "
        "WHERE i.business_id = :b AND i.product_id = :p AND l.is_default AND i.deleted_at IS NULL",
        {"b": t.business_id, "p": product_id},
    )
    return D(row[0]["unit_price"]) if row else None
