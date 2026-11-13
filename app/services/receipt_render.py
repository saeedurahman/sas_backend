"""Render the public receipt (mobile-friendly HTML and a narrow-roll PDF).

Only receipt content is rendered: business, branch, numbers, items, totals, payments and the
settings footer. Customer details, cashier, payment references and ledger data are never
included - the link is bearer-secret and may be forwarded.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from app.schemas.invoice import InvoiceData

_PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f2f2f2;font:15px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111}
.r{max-width:420px;margin:12px auto;background:#fff;padding:20px 18px;border-radius:8px}
h1{font-size:20px;margin:0 0 2px;text-align:center}
.c{text-align:center;color:#555;font-size:13px}
.v{display:inline-block;margin:8px 0;padding:2px 10px;border:2px solid #b00020;color:#b00020;font-weight:700}
hr{border:0;border-top:1px dashed #bbb;margin:12px 0}
table{width:100%;border-collapse:collapse}
td,th{padding:3px 0;vertical-align:top}
th{text-align:left;font-size:12px;color:#666;font-weight:600}
.n{text-align:right;white-space:nowrap;padding-left:8px}
.t td{font-weight:700;font-size:17px;padding-top:6px}
.m{color:#666;font-size:12px}
.f{margin-top:14px;text-align:center;color:#444;white-space:pre-line}
"""


def money(value: Decimal) -> str:
    return f"{Decimal(value):.2f}"


def qty(value: Decimal) -> str:
    text = f"{Decimal(value):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def local_stamp(inv: InvoiceData, zone: ZoneInfo) -> str:
    return inv.sold_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _is_void(inv: InvoiceData) -> bool:
    return inv.status in ("voided", "void", "cancelled", "canceled")


def render_html(inv: InvoiceData, zone: ZoneInfo, pdf_href: str | None = None) -> str:
    e = escape
    currency = e(inv.business.currency or "PKR")
    rows = []
    for ln in inv.lines:
        name = ln.product_name + (f" ({ln.variation_name})" if ln.variation_name else "")
        extra = f"{qty(ln.qty)} x {money(ln.unit_price)}"
        if ln.discount_amount:
            extra += f" &minus;{money(ln.discount_amount)}"
        rows.append(
            f"<tr><td>{e(name)}<div class='m'>{extra}</div></td>"
            f"<td class='n'>{money(ln.line_total)}</td></tr>"
        )
    pays = "".join(
        f"<tr><td>{e(str(p.payment_method).replace('_', ' ').title())}</td>"
        f"<td class='n'>{money(p.amount)}</td></tr>"
        for p in inv.payments
    )
    s = inv.summary
    totals = f"<tr><td>Subtotal</td><td class='n'>{money(s.subtotal)}</td></tr>"
    if s.total_discount:
        totals += f"<tr><td>Discount</td><td class='n'>&minus;{money(s.total_discount)}</td></tr>"
    if s.total_tax:
        totals += f"<tr><td>Tax</td><td class='n'>{money(s.total_tax)}</td></tr>"
    totals += (
        f"<tr class='t'><td>Total ({currency})</td><td class='n'>{money(s.total_amount)}</td></tr>"
        f"<tr><td>Paid</td><td class='n'>{money(s.total_paid)}</td></tr>"
    )
    if s.balance_due:
        totals += f"<tr><td>Balance due</td><td class='n'>{money(s.balance_due)}</td></tr>"
    if s.change_given:
        totals += f"<tr><td>Change</td><td class='n'>{money(s.change_given)}</td></tr>"

    numbers = f"Receipt # {e(inv.sale_number)}"
    if inv.client_receipt_no and inv.client_receipt_no != inv.sale_number:
        numbers += f"<br>Device receipt # {e(inv.client_receipt_no)}"
    address = " &middot; ".join(e(x) for x in (inv.branch_address, inv.branch_phone) if x)
    void = "<div class='c'><span class='v'>VOID</span></div>" if _is_void(inv) else ""
    footer = f"<div class='f'>{e(inv.receipt_footer)}</div>" if inv.receipt_footer else ""
    pdf = f"<p class='c'><a href='{e(pdf_href)}'>Download PDF</a></p>" if pdf_href else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        f"<title>Receipt {e(inv.sale_number)}</title><style>{_PAGE_CSS}</style></head><body>"
        f"<div class='r'><h1>{e(inv.business.name)}</h1>"
        f"<div class='c'>{e(inv.branch_name)}</div>"
        + (f"<div class='c'>{address}</div>" if address else "")
        + f"{void}<hr><div class='m'>{numbers}<br>{e(local_stamp(inv, zone))}</div><hr>"
        f"<table><tr><th>Item</th><th class='n'>{currency}</th></tr>{''.join(rows)}</table><hr>"
        f"<table>{totals}</table>"
        + (f"<hr><table><tr><th>Payments</th><th></th></tr>{pays}</table>" if pays else "")
        + f"{footer}{pdf}</div></body></html>"
    )


def error_page(message: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'><title>Receipt</title>"
        f"<style>{_PAGE_CSS}</style></head><body><div class='r'><h1>Receipt unavailable</h1>"
        f"<p class='c'>{escape(message)}</p></div></body></html>"
    )


def _latin(text: str) -> str:
    # Core PDF fonts are Latin-1 only; anything else becomes '?' instead of failing the render.
    return text.encode("latin-1", "replace").decode("latin-1")


def render_pdf(inv: InvoiceData, zone: ZoneInfo) -> bytes:
    from fpdf import FPDF

    width = 80.0
    margin = 5.0
    inner = width - 2 * margin
    s = inv.summary
    estimated = 70 + 9 * len(inv.lines) + 5 * len(inv.payments) + (14 if inv.receipt_footer else 0)
    pdf = FPDF(unit="mm", format=(width, max(120.0, float(estimated))))
    pdf.set_margins(margin, margin, margin)
    pdf.set_auto_page_break(auto=True, margin=margin)
    pdf.add_page()

    def centered(text: str, size: int = 9, bold: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.multi_cell(inner, 4.5, _latin(text), align="C", new_x="LMARGIN", new_y="NEXT")

    def pair(left: str, right: str, bold: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.cell(inner - 22, 4.5, _latin(left)[:38])
        pdf.cell(22, 4.5, right, align="R", new_x="LMARGIN", new_y="NEXT")

    def rule() -> None:
        y = pdf.get_y() + 1
        pdf.line(margin, y, width - margin, y)
        pdf.ln(2.5)

    centered(inv.business.name, 12, True)
    centered(inv.branch_name)
    for extra in (inv.branch_address, inv.branch_phone):
        if extra:
            centered(extra, 8)
    if _is_void(inv):
        centered("*** VOID ***", 11, True)
    rule()
    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(inner, 4, _latin(f"Receipt # {inv.sale_number}"), new_x="LMARGIN", new_y="NEXT")
    if inv.client_receipt_no and inv.client_receipt_no != inv.sale_number:
        pdf.multi_cell(
            inner, 4, _latin(f"Device receipt # {inv.client_receipt_no}"),
            new_x="LMARGIN", new_y="NEXT",
        )
    pdf.multi_cell(inner, 4, local_stamp(inv, zone), new_x="LMARGIN", new_y="NEXT")
    rule()
    for ln in inv.lines:
        name = ln.product_name + (f" ({ln.variation_name})" if ln.variation_name else "")
        pair(name, money(ln.line_total))
        pdf.set_font("Helvetica", "", 7)
        pdf.cell(inner, 3.5, f"  {qty(ln.qty)} x {money(ln.unit_price)}", new_x="LMARGIN", new_y="NEXT")
    rule()
    pair("Subtotal", money(s.subtotal))
    if s.total_discount:
        pair("Discount", "-" + money(s.total_discount))
    if s.total_tax:
        pair("Tax", money(s.total_tax))
    pair(f"TOTAL ({inv.business.currency or 'PKR'})", money(s.total_amount), bold=True)
    pair("Paid", money(s.total_paid))
    if s.balance_due:
        pair("Balance due", money(s.balance_due))
    if s.change_given:
        pair("Change", money(s.change_given))
    if inv.payments:
        rule()
        for p in inv.payments:
            pair(str(p.payment_method).replace("_", " ").title(), money(p.amount))
    if inv.receipt_footer:
        rule()
        centered(inv.receipt_footer, 8)
    return bytes(pdf.output())
