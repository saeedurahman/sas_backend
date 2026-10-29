"""Builders for offline-sync tests (op envelopes, push/pull wrappers)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from tests.helpers.tenants import TenantContext


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ago(**delta: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**delta)


def op(
    op_type: str,
    payload: dict[str, Any],
    *,
    op_id: str | None = None,
    occurred_at: datetime | None = None,
    depends_on: list[str] | str | None = None,
) -> dict[str, Any]:
    moment = occurred_at or ago(seconds=0)
    body: dict[str, Any] = {
        "op_id": op_id or str(uuid.uuid4()),
        "type": op_type,
        "client_created_at": iso(moment),
        "occurred_at": iso(moment),
        "payload": payload,
    }
    if depends_on is not None:
        body["depends_on"] = depends_on
    return body


def sale_payload(
    t: TenantContext,
    *,
    sale_id: str | None = None,
    receipt: str | None = None,
    qty: str = "1",
    unit_price: str = "100.00",
    tax_amount: str = "0",
    tax_rate: str = "0",
    discount_amount: str = "0",
    payments: list[dict] | None = None,
    shift_id: str | None = "default",
    customer_id: str | None = None,
    product_id: str | None = None,
    branch_id: str | None = None,
    lines: list[dict] | None = None,
) -> dict[str, Any]:
    total = Decimal(qty) * Decimal(unit_price) - Decimal(discount_amount) + Decimal(tax_amount)
    body: dict[str, Any] = {
        "id": sale_id or str(uuid.uuid4()),
        "client_receipt_no": receipt or f"D1-{uuid.uuid4().hex[:8]}",
        "branch_id": branch_id or t.branch_id,
        "register_shift_id": t.shift_id if shift_id == "default" else shift_id,
        "lines": lines
        or [
            {
                "product_id": product_id or t.product_id,
                "qty": qty,
                "unit_price": unit_price,
                "discount_amount": discount_amount,
                "tax_rate": tax_rate,
                "tax_amount": tax_amount,
            }
        ],
        "payments": payments
        if payments is not None
        else [{"payment_method": "cash", "amount": str(total.quantize(Decimal("0.01")))}],
    }
    if customer_id:
        body["customer_id"] = customer_id
    return body


def push(
    client: httpx.Client,
    t: TenantContext,
    ops: list[dict[str, Any]],
    *,
    device_id: str = "dev-1",
    headers: dict[str, str] | None = None,
    batch_id: str | None = None,
    **extra: Any,
) -> httpx.Response:
    return client.post(
        t.url("/sync/push"),
        headers=headers or t.owner_headers,
        json={"device_id": device_id, "batch_id": batch_id or str(uuid.uuid4()), "ops": ops, **extra},
    )


def push_ok(client: httpx.Client, t: TenantContext, ops: list[dict], **kwargs: Any) -> list[dict]:
    resp = push(client, t, ops, **kwargs)
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == len(ops)
    return results


def pull(
    client: httpx.Client,
    t: TenantContext,
    *,
    since: str | None = None,
    limit: int | None = None,
    device_id: str = "dev-1",
    headers: dict[str, str] | None = None,
    branch_id: str | None = None,
) -> httpx.Response:
    params: dict[str, Any] = {"device_id": device_id}
    if since:
        params["since"] = since
    if limit:
        params["limit"] = limit
    if branch_id:
        params["branch_id"] = branch_id
    return client.get(t.url("/sync/pull"), headers=headers or t.owner_headers, params=params)


def pull_json(client: httpx.Client, t: TenantContext, **kwargs: Any) -> dict[str, Any]:
    resp = pull(client, t, **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def pull_all(
    client: httpx.Client, t: TenantContext, since: str | None = None, **kwargs: Any
) -> tuple[list[dict], str]:
    """Follow has_more to the end; return (all changes, final cursor)."""
    changes: list[dict] = []
    cursor = since
    while True:
        page = pull_json(client, t, since=cursor, **kwargs)
        assert not page["full_resync_required"], page
        changes.extend(page["changes"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            return changes, cursor


def snapshot_all(
    client: httpx.Client,
    t: TenantContext,
    *,
    limit: int = 500,
    device_id: str = "dev-1",
    headers: dict[str, str] | None = None,
) -> tuple[list[dict], str, int]:
    changes: list[dict] = []
    token = None
    pages = 0
    while True:
        params: dict[str, Any] = {"device_id": device_id, "limit": limit}
        if token:
            params["page_token"] = token
        resp = client.get(t.url("/sync/snapshot"), headers=headers or t.owner_headers, params=params)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        pages += 1
        changes.extend(page["changes"])
        cursor = page["cursor"]
        token = page["next_page_token"]
        if not page["has_more"]:
            return changes, cursor, pages


def by_entity(changes: list[dict], entity: str) -> list[dict]:
    return [c for c in changes if c["entity"] == entity]
