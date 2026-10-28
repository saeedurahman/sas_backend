"""Turn the tenders on a sale into the payments that are actually applied.

Pure money math, no I/O:

* cash may exceed what is still due; the surplus is *change* and never reaches
  the till (only the net cash does),
* every other method (card, wallet, credit, ...) must fit within the amount due,
* a sale that is not free needs at least one payment.
"""

from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status

from app.models.enums import PaymentMethodEnum

_ZERO = Decimal("0")


@dataclass(frozen=True)
class AppliedPayment:
    method: PaymentMethodEnum
    amount: Decimal  # applied to the sale (net of change)
    tendered: Decimal  # what the customer handed over
    change: Decimal
    reference_no: str | None
    paid_at: object | None  # datetime | None, passed through untouched


@dataclass(frozen=True)
class Settlement:
    payments: list[AppliedPayment]
    total_applied: Decimal
    change_given: Decimal
    credit_amount: Decimal


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def settle_payments(
    total_due: Decimal,
    payments: list,
    *,
    require_payment: bool = True,
) -> Settlement:
    """``payments``: objects with ``payment_method``, ``amount``, ``reference_no``, ``paid_at``."""
    if not payments:
        if require_payment and total_due > _ZERO:
            raise _bad_request("At least one payment is required")
        return Settlement([], _ZERO, _ZERO, _ZERO)

    non_cash = _ZERO
    cash_tendered = _ZERO
    for payment in payments:
        if payment.amount <= _ZERO:
            raise _bad_request("Payment amounts must be greater than 0")
        if payment.payment_method == PaymentMethodEnum.cash:
            cash_tendered += payment.amount
        else:
            non_cash += payment.amount

    if non_cash > total_due:
        raise _bad_request(
            f"Non-cash payments ({non_cash}) exceed the amount due ({total_due})"
        )
    cash_due = total_due - non_cash
    if cash_tendered > _ZERO and cash_due <= _ZERO:
        raise _bad_request("Cash was tendered but nothing is left to pay in cash")

    cash_applied_total = min(cash_tendered, cash_due)
    change_total = cash_tendered - cash_applied_total

    applied: list[AppliedPayment] = []
    cash_left = cash_applied_total
    last_cash_index: int | None = None
    for payment in payments:
        if payment.payment_method != PaymentMethodEnum.cash:
            applied.append(
                AppliedPayment(
                    method=payment.payment_method,
                    amount=payment.amount,
                    tendered=payment.amount,
                    change=_ZERO,
                    reference_no=payment.reference_no,
                    paid_at=payment.paid_at,
                )
            )
            continue
        take = min(payment.amount, cash_left)
        cash_left -= take
        if take <= _ZERO:
            # A cash row entirely absorbed by change: its tender is folded into
            # the previous cash row's change below.
            continue
        applied.append(
            AppliedPayment(
                method=payment.payment_method,
                amount=take,
                tendered=payment.amount,
                change=payment.amount - take,
                reference_no=payment.reference_no,
                paid_at=payment.paid_at,
            )
        )
        last_cash_index = len(applied) - 1

    accounted = sum((p.change for p in applied), _ZERO)
    if accounted != change_total and last_cash_index is not None:
        last = applied[last_cash_index]
        extra = change_total - accounted
        applied[last_cash_index] = AppliedPayment(
            method=last.method,
            amount=last.amount,
            tendered=last.tendered + extra,
            change=last.change + extra,
            reference_no=last.reference_no,
            paid_at=last.paid_at,
        )

    total_applied = sum((p.amount for p in applied), _ZERO)
    credit_amount = sum(
        (p.amount for p in applied if p.method == PaymentMethodEnum.credit), _ZERO
    )
    return Settlement(
        payments=applied,
        total_applied=total_applied,
        change_given=change_total,
        credit_amount=credit_amount,
    )
