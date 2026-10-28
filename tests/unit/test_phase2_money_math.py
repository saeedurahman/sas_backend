"""Phase 2 pure-math tests: settlement, return proration, FIFO costing, time, caches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.core import permission_cache
from app.core.idempotency import request_fingerprint
from app.models.enums import PaymentMethodEnum
from app.services import business_time
from app.services.fifo_service import (
    FifoLayerConsumption,
    compute_weighted_cost_per_unit,
    plan_lifo_restoration,
)
from app.services.pricing_engine import determine_sale_status
from app.services.return_service import compute_return_line_amounts
from app.services.settlement import settle_payments

D = Decimal


@dataclass
class Pay:
    payment_method: PaymentMethodEnum
    amount: Decimal
    reference_no: str | None = None
    paid_at: object | None = None


def cash(a):
    return Pay(PaymentMethodEnum.cash, D(a))


def card(a):
    return Pay(PaymentMethodEnum.card, D(a))


def credit(a):
    return Pay(PaymentMethodEnum.credit, D(a))


# -- settlement ----------------------------------------------------------------


def test_cash_overpayment_becomes_change():
    s = settle_payments(D("80.00"), [cash("100")])
    assert s.total_applied == D("80.00")
    assert s.change_given == D("20")
    assert s.payments[0].amount == D("80.00")
    assert s.payments[0].tendered == D("100")
    assert s.payments[0].change == D("20")


def test_exact_cash_has_no_change():
    s = settle_payments(D("50"), [cash("50")])
    assert s.change_given == 0 and s.total_applied == D("50")


def test_split_card_then_cash_change_only_on_cash():
    s = settle_payments(D("100"), [card("60"), cash("50")])
    assert s.total_applied == D("100")
    assert s.change_given == D("10")
    assert [p.amount for p in s.payments] == [D("60"), D("40")]


def test_non_cash_overpayment_rejected():
    with pytest.raises(HTTPException) as e:
        settle_payments(D("50"), [card("60")])
    assert e.value.status_code == 400


def test_cash_when_card_covers_all_rejected():
    with pytest.raises(HTTPException):
        settle_payments(D("50"), [card("50"), cash("5")])


def test_credit_counts_only_applied_credit():
    s = settle_payments(D("100"), [cash("30"), credit("70")])
    assert s.credit_amount == D("70")
    assert s.total_applied == D("100")


def test_no_payment_rejected_unless_free():
    with pytest.raises(HTTPException):
        settle_payments(D("10"), [])
    assert settle_payments(D("0"), []).payments == []


def test_two_cash_rows_surplus_folded_into_change():
    s = settle_payments(D("30"), [cash("30"), cash("20")])
    assert s.total_applied == D("30")
    assert s.change_given == D("20")
    assert sum(p.change for p in s.payments) == D("20")
    assert all(p.amount > 0 for p in s.payments)


def test_partial_payment_allowed():
    s = settle_payments(D("100"), [cash("40")])
    assert s.total_applied == D("40") and s.change_given == 0


def test_status_free_sale_is_completed_and_unpaid_is_draft():
    assert determine_sale_status(D("0"), D("0")).value == "completed"
    assert determine_sale_status(D("10"), D("0")).value == "draft"
    assert determine_sale_status(D("10"), D("4")).value == "partially_paid"
    assert determine_sale_status(D("10"), D("10")).value == "completed"


# -- return proration ------------------------------------------------------------


def _ret(rq, oq, od, ot, prq=0, prd=0, prt=0):
    return compute_return_line_amounts(
        return_qty=D(rq),
        original_qty=D(oq),
        original_discount=D(od),
        original_tax=D(ot),
        returned_qty=D(prq),
        returned_discount=D(prd),
        returned_tax=D(prt),
    )


def test_partial_return_prorates_discount_and_tax():
    assert _ret(1, 3, "9.00", "6.00") == (D("3.00"), D("2.00"))


def test_final_return_takes_remainder_so_cents_balance():
    d1, t1 = _ret(1, 3, "10.00", "1.00")  # 3.33 / 0.33
    d2, t2 = _ret(1, 3, "10.00", "1.00", 1, d1, t1)
    d3, t3 = _ret(1, 3, "10.00", "1.00", 2, d1 + d2, t1 + t2)
    assert d1 + d2 + d3 == D("10.00")
    assert t1 + t2 + t3 == D("1.00")


def test_full_return_in_one_go_refunds_exact_line():
    assert _ret(2, 2, "5.55", "3.33") == (D("5.55"), D("3.33"))


# -- FIFO costing ----------------------------------------------------------------


def _slice(q, c):
    return FifoLayerConsumption(purchase_line_id=uuid4(), qty=D(q), cost_per_unit=D(c))


def test_weighted_cost_keeps_four_decimals():
    # (1*10 + 2*10.0001)/3 = 10.00006667 -> 10.0001 ; 2 dp would give 10.00
    cost = compute_weighted_cost_per_unit(D("3"), [_slice(1, "10"), _slice(2, "10.0001")])
    assert cost == D("10.0001")
    assert cost.as_tuple().exponent == -4


def test_weighted_cost_third():
    assert compute_weighted_cost_per_unit(
        D("3"), [_slice(1, "1"), _slice(2, "2")]
    ) == D("1.6667")


def test_lifo_restoration_unwinds_newest_first():
    a, b = _slice(2, "5"), _slice(3, "7")
    plan = plan_lifo_restoration([a, b], D("4"))
    assert [(p.purchase_line_id, p.qty) for p in plan] == [
        (b.purchase_line_id, D("3")),
        (a.purchase_line_id, D("1")),
    ]


# -- business time -----------------------------------------------------------------


def test_document_day_follows_karachi_not_utc():
    khi = ZoneInfo("Asia/Karachi")
    late_utc = datetime(2026, 3, 1, 20, 30, tzinfo=timezone.utc)  # 01:30 on 2 Mar in PKT
    assert business_time.local_date_key(khi, late_utc) == "20260302"
    assert business_time.local_date_key(ZoneInfo("UTC"), late_utc) == "20260301"


def test_day_bounds_in_utc():
    khi = ZoneInfo("Asia/Karachi")
    start, end = business_time.local_day_bounds_utc(date(2026, 3, 2), khi)
    assert start == datetime(2026, 3, 1, 19, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 2, 19, 0, tzinfo=timezone.utc)


def test_unknown_timezone_falls_back_to_karachi():
    assert business_time.load_zone("Nope/Nowhere").key == "Asia/Karachi"
    assert business_time.load_zone(None).key == "Asia/Karachi"


# -- caches / idempotency ------------------------------------------------------------


def test_permission_cache_hit_expiry_and_invalidation(monkeypatch):
    uid = uuid4()
    permission_cache.invalidate_all()
    assert permission_cache.get(uid) is None
    permission_cache.put(uid, frozenset({"sales.view"}))
    assert permission_cache.get(uid) == frozenset({"sales.view"})
    permission_cache.invalidate_all()
    assert permission_cache.get(uid) is None

    permission_cache.put(uid, frozenset({"a"}))
    real = permission_cache.time.monotonic
    monkeypatch.setattr(permission_cache.time, "monotonic", lambda: real() + 60)
    assert permission_cache.get(uid) is None


def test_fingerprint_stable_and_sensitive():
    a = request_fingerprint("POST", "/api/v1/sales", {"x": 1, "y": [1, 2]})
    assert a == request_fingerprint("post", "/api/v1/sales", {"y": [1, 2], "x": 1})
    assert a != request_fingerprint("POST", "/api/v1/sales", {"x": 2, "y": [1, 2]})
    assert a != request_fingerprint("POST", "/api/v1/returns", {"x": 1, "y": [1, 2]})
