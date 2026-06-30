"""Concurrent cancel vs return race tests (migrated from test_cancel_return_concurrency)."""

from __future__ import annotations

import os
import threading
import uuid

import httpx
import pytest

from tests.helpers.db import db_scalar
from tests.helpers.tenants import TenantContext, build_cancel_race_tenant

pytestmark = [pytest.mark.integration, pytest.mark.concurrency, pytest.mark.slow]

ITERATIONS = int(os.environ.get("CANCEL_RETURN_ITERATIONS", "30"))


@pytest.fixture(scope="module")
def cancel_race_tenant(require_server, api_base: str) -> TenantContext:
    suffix = uuid.uuid4().int % 10_000_000
    with httpx.Client(timeout=120.0) as client:
        return build_cancel_race_tenant(client, api_base, suffix)


def _create_partially_paid_sale(client: httpx.Client, tenant: TenantContext) -> dict[str, str]:
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None
    assert tenant.shift_id is not None
    sale = client.post(
        tenant.url("/sales"),
        headers=tenant.manager_headers,
        json={
            "branch_id": tenant.branch_id,
            "register_shift_id": tenant.shift_id,
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "2",
                    "unit_price": "50.00",
                }
            ],
            "payments": [{"payment_method": "cash", "amount": "40.00"}],
        },
    )
    assert sale.status_code == 201, sale.text
    body = sale.json()
    assert body["status"] == "partially_paid"
    return {"sale_id": body["id"], "sale_line_id": body["lines"][0]["id"]}


def _sale_status(sale_id: str) -> str | None:
    return db_scalar(
        "SELECT status FROM sales WHERE id = :sid AND deleted_at IS NULL",
        {"sid": sale_id},
    )


def _return_line_count(sale_id: str) -> int:
    count = db_scalar(
        """
        SELECT COUNT(*)
        FROM sale_return_lines srl
        JOIN sale_returns sr ON sr.id = srl.sale_return_id
        WHERE sr.sale_id = :sid
          AND sr.deleted_at IS NULL
          AND srl.deleted_at IS NULL
        """,
        {"sid": sale_id},
    )
    return int(count or 0)


def _corruption_present(sale_id: str) -> bool:
    return _sale_status(sale_id) == "cancelled" and _return_line_count(sale_id) > 0


def _run_iteration(
    client: httpx.Client,
    tenant: TenantContext,
    iteration: int,
    stats: dict[str, int],
) -> None:
    sale_info = _create_partially_paid_sale(client, tenant)
    sale_id = sale_info["sale_id"]
    sale_line_id = sale_info["sale_line_id"]
    assert tenant.manager_headers is not None

    barrier = threading.Barrier(2)
    outcomes: dict[str, httpx.Response] = {}

    def cancel_worker() -> None:
        barrier.wait()
        with httpx.Client(timeout=120.0) as thread_client:
            outcomes["cancel"] = thread_client.put(
                tenant.url(f"/sales/{sale_id}/cancel"),
                headers=tenant.manager_headers,
            )

    def return_worker() -> None:
        barrier.wait()
        with httpx.Client(timeout=120.0) as thread_client:
            outcomes["return"] = thread_client.post(
                tenant.url("/returns"),
                headers=tenant.manager_headers,
                json={
                    "branch_id": tenant.branch_id,
                    "sale_id": sale_id,
                    "lines": [
                        {
                            "product_id": tenant.product_id,
                            "sale_line_id": sale_line_id,
                            "qty": "1",
                            "unit_price": "50.00",
                        }
                    ],
                    "refund_payments": [{"payment_method": "cash", "amount": "50.00"}],
                },
            )

    t_cancel = threading.Thread(target=cancel_worker)
    t_return = threading.Thread(target=return_worker)
    t_cancel.start()
    t_return.start()
    t_cancel.join()
    t_return.join()

    cancel_resp = outcomes["cancel"]
    return_resp = outcomes["return"]
    cancel_ok = cancel_resp.status_code == 200
    return_ok = return_resp.status_code == 201

    assert cancel_resp.status_code < 500 and return_resp.status_code < 500, (
        f"iter {iteration}: cancel={cancel_resp.status_code} return={return_resp.status_code} "
        f"cancel_body={cancel_resp.text[:200]!r} return_body={return_resp.text[:200]!r}"
    )
    assert not (cancel_ok and return_ok), f"iter {iteration}: both cancel and return succeeded"
    assert not _corruption_present(sale_id), (
        f"iter {iteration}: corruption status={_sale_status(sale_id)!r} "
        f"return_lines={_return_line_count(sale_id)}"
    )

    if cancel_ok and not return_ok:
        stats["cancel_wins"] += 1
    elif return_ok and not cancel_ok:
        stats["return_wins"] += 1
    else:
        stats["other"] += 1


def test_cancel_return_race_invariants(
    cancel_race_tenant: TenantContext,
) -> None:
    stats = {"cancel_wins": 0, "return_wins": 0, "other": 0}
    with httpx.Client(timeout=120.0) as client:
        for i in range(1, ITERATIONS + 1):
            _run_iteration(client, cancel_race_tenant, i, stats)

    assert stats["cancel_wins"] + stats["return_wins"] + stats["other"] == ITERATIONS
