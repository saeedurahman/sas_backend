"""Public receipt links: issue, render, expiry, revocation, privacy, rate limit."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.sales import create_sale_payload
from tests.helpers.server import managed_server
from tests.helpers.sqlexec import sql
from tests.helpers.tenants import build_pos_tenant

pytestmark = pytest.mark.integration

PHONE = "03451234567"


def _sale(c: httpx.Client, t, customer_id: str | None = None) -> dict:
    r = c.post(
        t.url("/sales"), headers=t.owner_headers,
        json=create_sale_payload(branch_id=t.branch_id, product_id=t.product_id, qty="2",
                                 unit_price="150.00", register_shift_id=t.shift_id,
                                 customer_id=customer_id),
    )
    assert_status(r, 201, label="sale")
    return r.json()


@pytest.fixture
def ctx(http_client, api_base, unique_suffix):
    t = build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="472")
    cust = http_client.post(t.url("/customers"), headers=t.owner_headers,
                            json={"name": "Secret Customer", "phone": PHONE})
    assert_status(cust, 201, label="customer")
    sale = _sale(http_client, t, cust.json()["id"])
    return t, sale


def _link(c, t, sale_id) -> dict:
    r = c.post(t.url(f"/sales/{sale_id}/receipt-link"), headers=t.owner_headers)
    assert_status(r, 201, label="link")
    return r.json()


def test_link_shape_and_html_receipt(http_client, ctx):
    t, sale = ctx
    link = _link(http_client, t, sale["id"])
    assert set(link) >= {"url", "expires_at"}
    assert "/r/" in link["url"]
    r = http_client.get(link["url"])  # no auth header
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "viewport" in r.text
    assert str(sale["sale_number"]) in r.text
    assert "300.00" in r.text
    assert "no-store" in r.headers["cache-control"]
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_no_customer_data_in_receipt(http_client, ctx):
    t, sale = ctx
    link = _link(http_client, t, sale["id"])
    for suffix in ("", "?format=pdf"):
        body = http_client.get(link["url"] + suffix).text
        assert PHONE not in body and "Secret Customer" not in body
        assert "3451234567" not in body


def test_pdf_format(http_client, ctx):
    t, sale = ctx
    link = _link(http_client, t, sale["id"])
    r = http_client.get(link["url"], params={"format": "pdf"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_expired_link_is_410(http_client, ctx):
    t, sale = ctx
    link = _link(http_client, t, sale["id"])
    sql("UPDATE receipt_links SET expires_at = now() - interval '1 day' WHERE sale_id = :s",
        s=sale["id"])
    assert http_client.get(link["url"]).status_code == 410


def test_revoked_link_is_404_and_new_link_works(http_client, ctx):
    t, sale = ctx
    link = _link(http_client, t, sale["id"])
    r = http_client.delete(t.url(f"/sales/{sale['id']}/receipt-link"), headers=t.owner_headers)
    assert r.status_code == 200 and r.json()["revoked"] >= 1
    assert http_client.get(link["url"]).status_code == 404
    fresh = _link(http_client, t, sale["id"])
    assert http_client.get(fresh["url"]).status_code == 200


def test_forged_and_garbage_tokens_are_404(http_client, api_base, ctx):
    t, sale = ctx
    link = _link(http_client, t, sale["id"])
    root = link["url"].split("/r/")[0]
    token = link["url"].split("/r/")[1]
    forged = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    for bad in (forged, "garbage", "a.b", token.split(".")[0] + ".AAAAAAAAAAAAAAAAAAAAAA"):
        assert http_client.get(f"{root}/r/{bad}").status_code == 404


def test_requires_permission(http_client, ctx):
    t, sale = ctx
    assert http_client.post(t.url(f"/sales/{sale['id']}/receipt-link")).status_code in (401, 403)
    r = http_client.post(t.url(f"/sales/{sale['id']}/receipt-link"), headers=t.cashier_headers)
    assert r.status_code in (201, 403, 404)  # cashiers hold sales.view; foreign branch rules apply


def test_other_tenant_cannot_link(http_client, api_base, ctx, unique_suffix):
    t, sale = ctx
    other = build_pos_tenant(http_client, api_base, (unique_suffix + 1) % 10_000_000,
                             phone_prefix="473")
    r = http_client.post(other.url(f"/sales/{sale['id']}/receipt-link"), headers=other.owner_headers)
    assert r.status_code == 404


def test_lookup_by_number_and_client_receipt_no(http_client, ctx):
    t, sale = ctx
    sql("UPDATE sales SET client_receipt_no = :n WHERE id = :s", n="OFF-0001", s=sale["id"])
    for body in ({"sale_number": sale["sale_number"]}, {"client_receipt_no": "OFF-0001"}):
        r = http_client.post(t.url("/sales/receipt-link/lookup"), headers=t.owner_headers, json=body)
        assert r.status_code in (200, 201), r.text
        assert http_client.get(r.json()["url"]).status_code == 200
    r = http_client.post(t.url("/sales/receipt-link/lookup"), headers=t.owner_headers,
                         json={"client_receipt_no": "NOPE"})
    assert r.status_code == 404


def test_receipt_link_rate_limit(unique_suffix):
    with managed_server({"RATE_LIMIT_RECEIPT_IP": "3/minute"}) as base:
        with httpx.Client(timeout=60) as c:
            t = build_pos_tenant(c, base, unique_suffix, phone_prefix="474")
            sale = _sale(c, t)
            link = _link(c, t, sale["id"])
            codes = [c.get(link["url"]).status_code for _ in range(6)]
            assert codes[:3] == [200, 200, 200]
            assert 429 in codes
