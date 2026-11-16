"""Pricing engine: exclusive vs tax-inclusive pricing, cart discounts, grand-total rounding."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from app.services.pricing_engine import (
    calculate_line_total,
    calculate_sale_totals,
    distribute_cart_discount,
    price_cart,
    round_total,
)

D = Decimal
ZERO = D("0")


def line(qty="1", price="100", rate="0", *, pct="0", amount="0", inclusive=False):
    return calculate_line_total(D(qty), D(price), D(pct), D(amount), D(rate), inclusive=inclusive)


# ── the worked example: price 6 at 17% ───────────────────────────────────────


@pytest.mark.parametrize(
    "qty, net, tax, total",
    [("1", "6.00", "1.02", "7.02"), ("2", "12.00", "2.04", "14.04"), ("3", "18.00", "3.06", "21.06")],
)
def test_exclusive_six_at_17(qty, net, tax, total):
    r = line(qty, "6", "17")
    assert (r["net_amount"], r["tax_amount"], r["line_total"]) == (D(net), D(tax), D(total))


def test_inclusive_six_at_17():
    r = line("1", "6", "17", inclusive=True)
    assert r["line_total"] == D("6.00")
    assert r["tax_amount"] == D("0.87")
    assert r["net_amount"] == D("5.13")


def test_inclusive_quantities_keep_gross():
    assert line("2", "6", "17", inclusive=True)["line_total"] == D("12.00")
    assert line("3", "6", "17", inclusive=True)["line_total"] == D("18.00")


# ── matrix: rates x quantities x mode ────────────────────────────────────────


@pytest.mark.parametrize("rate", ["0", "5", "10", "17"])
@pytest.mark.parametrize("qty", ["1", "2", "3", "0.5"])
@pytest.mark.parametrize("inclusive", [False, True])
def test_net_plus_tax_is_always_the_gross(rate, qty, inclusive):
    r = line(qty, "123.45", rate, inclusive=inclusive)
    assert r["net_amount"] + r["tax_amount"] == r["line_total"]
    assert r["net_amount"] >= 0 and r["tax_amount"] >= 0
    if inclusive:
        assert r["line_total"] == r["line_subtotal"] - r["effective_discount"]
    else:
        assert r["net_amount"] == r["line_subtotal"] - r["effective_discount"]


@pytest.mark.parametrize("inclusive", [False, True])
def test_zero_rate_is_identical_in_both_modes(inclusive):
    assert line("3", "19.99", "0", inclusive=inclusive) == line("3", "19.99", "0", inclusive=False)


def test_exclusive_formulas_unchanged():
    r = line("2", "50.00", "10", amount="10")
    assert r["line_subtotal"] == D("100.00")
    assert r["net_amount"] == D("90.00")
    assert r["tax_amount"] == D("9.00")
    assert r["line_total"] == D("99.00")


def test_inclusive_discount_applies_to_tax_inclusive_price():
    r = line("2", "50.00", "10", amount="10", inclusive=True)
    assert r["line_total"] == D("90.00")  # 100 - 10, tax already inside
    assert r["tax_amount"] == D("8.18")  # 90 * 10 / 110 = 8.1818
    assert r["net_amount"] == D("81.82")


def test_percent_discount_both_modes():
    ex = line("1", "200", "10", pct="25")
    assert (ex["effective_discount"], ex["net_amount"], ex["tax_amount"]) == (D("50.00"), D("150.00"), D("15.00"))
    inc = line("1", "200", "10", pct="25", inclusive=True)
    assert (inc["effective_discount"], inc["line_total"]) == (D("50.00"), D("150.00"))


def test_discount_never_makes_line_negative():
    for inclusive in (False, True):
        r = line("1", "10", "17", amount="50", inclusive=inclusive)
        assert r["line_total"] == ZERO and r["net_amount"] == ZERO and r["tax_amount"] == ZERO


def test_half_up_tax_rounding():
    # 0.5 * 1.01 = 0.505 -> 0.51 (half up); tax 5% of 0.51 = 0.0255 -> 0.03
    r = line("1", "0.505", "5")
    assert r["line_subtotal"] == D("0.51")
    assert r["tax_amount"] == D("0.03")


# ── modifiers follow the product's mode ──────────────────────────────────────


def test_modifier_price_is_priced_like_the_unit_price():
    # unit price + modifier add up before pricing, so the mode applies to both.
    base, modifier = D("100"), D("20")
    inc = line("1", str(base + modifier), "17", inclusive=True)
    ex = line("1", str(base + modifier), "17", inclusive=False)
    assert inc["line_total"] == D("120.00")
    assert ex["line_total"] == D("140.40")
    assert inc["tax_amount"] == D("17.44")  # 120 * 17 / 117


# ── cart level discount ──────────────────────────────────────────────────────


def test_cart_discount_is_spread_proportionally_last_line_takes_remainder():
    shares = distribute_cart_discount([D("100"), D("100"), D("100")], D("10"))
    assert shares == [D("3.33"), D("3.33"), D("3.34")]
    assert sum(shares) == D("10")


def test_cart_discount_never_negative_or_above_line():
    shares = distribute_cart_discount([D("5"), D("0"), D("1")], D("100"))
    assert shares == [D("5"), D("0"), D("1")]
    assert distribute_cart_discount([D("10")], D("0")) == [ZERO]
    assert distribute_cart_discount([], D("5")) == []


def test_cart_discount_overflow_goes_to_lines_with_room():
    # last line can only absorb 1; the rest must land on the first line
    shares = distribute_cart_discount([D("99"), D("1")], D("50"))
    assert sum(shares) == D("50")
    assert all(s >= 0 for s in shares)
    assert shares[1] <= D("1")


@pytest.mark.parametrize("inclusive", [False, True])
def test_cart_discount_in_price_cart(inclusive):
    lines = [
        {"qty": D("1"), "unit_price": D("100"), "tax_rate": D("17")},
        {"qty": D("2"), "unit_price": D("50"), "tax_rate": D("5")},
    ]
    priced, totals = price_cart(lines, inclusive=inclusive, cart_discount=D("20"))
    assert totals["total_discount"] == D("20.00")
    assert sum(p["effective_discount"] for p in priced) == D("20.00")
    assert totals["exact_total"] == sum((p["net_amount"] + p["tax_amount"] for p in priced), ZERO)
    if inclusive:
        assert totals["exact_total"] == D("180.00")  # 200 - 20, tax inside


# ── grand-total rounding ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exact, mode, rounded, diff",
    [
        ("123.45", "none", "123.45", "0.00"),
        ("123.45", "nearest_1", "123.00", "-0.45"),
        ("123.50", "nearest_1", "124.00", "0.50"),  # half up
        ("123.55", "nearest_1", "124.00", "0.45"),
        ("123.45", "nearest_5", "125.00", "1.55"),
        ("122.50", "nearest_5", "125.00", "2.50"),  # half step rounds up
        ("122.49", "nearest_5", "120.00", "-2.49"),
        ("125.00", "nearest_5", "125.00", "0.00"),
        ("125.00", "nearest_10", "130.00", "5.00"),  # half step rounds up
        ("124.99", "nearest_10", "120.00", "-4.99"),
        ("0.40", "nearest_1", "0.00", "-0.40"),
        ("0.00", "nearest_10", "0.00", "0.00"),
    ],
)
def test_round_total(exact, mode, rounded, diff):
    got, amount = round_total(D(exact), mode)
    assert got == D(rounded) and amount == D(diff)
    assert got - D(exact) == amount


def test_unknown_mode_is_treated_as_none():
    assert round_total(D("10.37"), "bogus") == (D("10.37"), ZERO)
    assert round_total(D("10.37"), None) == (D("10.37"), ZERO)


def test_rounding_only_touches_the_grand_total_never_lines():
    lines = [
        {"qty": D("1"), "unit_price": D("6"), "tax_rate": D("17")},
        {"qty": D("3"), "unit_price": D("6"), "tax_rate": D("17")},
    ]
    priced_none, t_none = price_cart(lines, rounding_mode="none")
    priced_ten, t_ten = price_cart(lines, rounding_mode="nearest_10")
    assert priced_none == priced_ten  # per-line values identical
    assert t_none["exact_total"] == D("28.08")
    assert t_ten["exact_total"] == D("28.08")
    assert t_ten["total_amount"] == D("30.00") and t_ten["rounding_amount"] == D("1.92")
    assert t_ten["net_amount"] + t_ten["total_tax"] + t_ten["rounding_amount"] == t_ten["total_amount"]


def test_totals_reconcile_inclusive_with_rounding():
    priced, totals = price_cart(
        [{"qty": D("3"), "unit_price": D("6"), "tax_rate": D("17")}],
        inclusive=True,
        rounding_mode="nearest_5",
    )
    assert totals["exact_total"] == D("18.00")
    assert totals["total_amount"] == D("20.00") and totals["rounding_amount"] == D("2.00")
    assert totals["net_amount"] + totals["total_tax"] == totals["exact_total"]


def test_calculate_sale_totals_backward_compatible_keys():
    t = calculate_sale_totals([line("2", "6", "17")])
    assert {"subtotal", "total_discount", "total_tax", "total_amount"} <= set(t)
    assert t["total_amount"] == D("14.04") and t["rounding_amount"] == ZERO


# ── property style ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(30))
def test_random_carts_reconcile(seed):
    rnd = random.Random(seed)
    for _ in range(25):
        inclusive = rnd.choice([False, True])
        mode = rnd.choice(["none", "nearest_1", "nearest_5", "nearest_10"])
        lines = [
            {
                "qty": D(rnd.choice(["1", "2", "3", "0.5", "0.250", "7.125", "12"])),
                "unit_price": D(f"{rnd.randint(0, 99999) / 100:.2f}"),
                "discount_pct": D(rnd.choice(["0", "0", "5", "12.5", "100"])),
                "discount_amount": D(rnd.choice(["0", "0", "1.50", "9999"])),
                "tax_rate": D(rnd.choice(["0", "5", "10", "17", "12.375"])),
            }
            for _ in range(rnd.randint(1, 8))
        ]
        cart_discount = D(rnd.choice(["0", "0", "3.33", "250", "99999"]))
        priced, totals = price_cart(
            lines, inclusive=inclusive, rounding_mode=mode, cart_discount=cart_discount
        )
        for p in priced:
            # no negatives, and net + tax is the gross, exactly, in both modes
            assert min(p.values()) >= 0
            assert p["net_amount"] + p["tax_amount"] == p["line_total"]
        # sums of the lines are the totals
        assert totals["net_amount"] == sum((p["net_amount"] for p in priced), ZERO)
        assert totals["total_tax"] == sum((p["tax_amount"] for p in priced), ZERO)
        assert totals["total_discount"] == sum((p["effective_discount"] for p in priced), ZERO)
        assert totals["exact_total"] == sum((p["line_total"] for p in priced), ZERO)
        # rounding reconciles and stays within one step
        assert totals["total_amount"] == totals["exact_total"] + totals["rounding_amount"]
        step = {"none": ZERO, "nearest_1": D("1"), "nearest_5": D("5"), "nearest_10": D("10")}[mode]
        assert abs(totals["rounding_amount"]) <= step / 2
        assert totals["total_amount"] >= 0
        if mode != "none":
            assert totals["total_amount"] % step == 0
        # the cart discount never exceeds what the cart was worth after line discounts
        assert totals["total_discount"] <= totals["subtotal"]


@pytest.mark.parametrize("seed", range(10))
def test_zero_tax_totals_match_between_modes(seed):
    rnd = random.Random(1000 + seed)
    lines = [
        {
            "qty": D(rnd.choice(["1", "2", "0.5", "3"])),
            "unit_price": D(f"{rnd.randint(1, 50000) / 100:.2f}"),
            "discount_amount": D(rnd.choice(["0", "2.50"])),
            "tax_rate": ZERO,
        }
        for _ in range(rnd.randint(1, 6))
    ]
    for mode in ("none", "nearest_5"):
        assert price_cart(lines, inclusive=False, rounding_mode=mode)[1] == price_cart(
            lines, inclusive=True, rounding_mode=mode
        )[1]
