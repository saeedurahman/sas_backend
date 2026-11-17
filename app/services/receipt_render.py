"""One receipt rendering model shared by print, WhatsApp text and the public receipt page.

``build_receipt_model(sale, layout, channel)`` turns a sale (``InvoiceData``) and a layout
(business data, see ``receipt_layout_config``) into a neutral structure. The HTML, PDF
(fpdf2, Noto fonts, 58/80 mm exact height or A4) and plain-text renderers only consume that
structure, so they can never disagree about what a receipt says.

Rules baked into the model (all covered by tests):

* the customer sees ONE receipt number, chosen by ``meta.number_source``;
* internal sync / status text is never printed;
* the product name is the PRODUCT name; a variation is appended only when it has a real name;
* quantities have no decimals unless the unit allows them;
* the NTN line prints the business tax id under its own label, never a tax rate;
* tax and rounding come exactly from the sale snapshot;
* the QR carries the public receipt link and exists only when the sale is synced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.invoice import InvoiceData
from app.services.receipt_layout_config import (
    CHANNELS,
    LABEL_DEFAULTS,
    LayoutConfig,
    normalise_config,
)

MODEL_VERSION = 1
PAPER_WIDTH_MM = {"roll58": 58.0, "roll80": 80.0, "a4": 210.0}
_ZERO = Decimal("0")
_URDU = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

# ── value helpers ────────────────────────────────────────────────────────────


def money(value: Decimal | int | str) -> str:
    return f"{Decimal(value):.2f}"


def qty_text(value: Decimal, allows_decimal: bool) -> str:
    """No decimals for whole quantities; decimals only where the unit allows them."""
    value = Decimal(value)
    if allows_decimal:
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    elif value == value.to_integral_value():
        text = str(int(value))
    else:  # fractional quantity of a counted unit (legacy data): never hide it
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _rate_text(rate: Decimal) -> str:
    text = f"{Decimal(rate):.3f}".rstrip("0").rstrip(".")
    return text or "0"


_TAX_WORDS = re.compile(r"\b(gst|vat|sales\s*tax|tax|fbr|pra|srb|bra|kpra|rate)\b", re.I)


def looks_like_tax_text(value: str) -> bool:
    """True for text that is a tax label/rate ("GST 17%"), not a registration number."""
    text = value.strip()
    return "%" in text or bool(_TAX_WORDS.search(text)) and bool(re.search(r"\d", text))


def clean_ntn(value: str | None) -> str | None:
    """The business tax id as printed; a tax label/rate stored there by mistake is dropped."""
    if value is None:
        return None
    text = value.strip()
    if not text or looks_like_tax_text(text):
        return None
    return text


def _label(cfg: dict[str, Any], key: str) -> dict[str, str]:
    entry = cfg["labels"].get(key) or {}
    en_default, ur_default = LABEL_DEFAULTS.get(key, (key, ""))
    en = entry.get("en") or en_default
    ur = entry.get("ur") or ur_default
    text = f"{en} / {ur}" if en and ur and en != ur else (en or ur)
    return {"en": en, "ur": ur, "text": text}


def _expiry_text(value: date | None) -> str | None:
    return value.strftime("%m/%Y") if value else None


def pick_number(sale: InvoiceData, source: str) -> str | None:
    register = (sale.client_receipt_no or "").strip() or None
    official = (sale.sale_number or "").strip() or None
    return (register or official) if source == "register" else (official or register)


@dataclass(frozen=True)
class LayoutView:
    """The part of a layout the renderers need (id may be None for an unsaved preview)."""

    id: str | None
    name: str
    design: str
    paper: str
    config: dict[str, Any]

    @classmethod
    def build(cls, id: str | None, name: str, design: str, paper: str, raw: dict[str, Any] | None):
        return cls(id, name, design, paper, normalise_config(raw))


# ── the model ────────────────────────────────────────────────────────────────


def build_receipt_model(
    sale: InvoiceData,
    layout: LayoutView,
    channel: str,
    *,
    zone: ZoneInfo | None = None,
    qr_url: str | None = None,
) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    zone = zone or ZoneInfo("Asia/Karachi")
    cfg = layout.config
    parsed = LayoutConfig.model_validate(cfg)
    header, meta_cfg, items_cfg, totals_cfg, footer_cfg = (
        parsed.header, parsed.meta, parsed.items, parsed.totals, parsed.footer,
    )
    s = sale.summary
    public = channel == "link"  # bearer link: never customer, cashier or ledger data

    # header
    address = sale.branch_address or sale.business.address
    phone = sale.branch_phone or sale.business.phone
    ntn_value = clean_ntn(sale.business.tax_id)
    header_model = {
        "logo_url": sale.business.logo_url if header.show_logo else None,
        "business_name": sale.business.name if header.show_business_name else None,
        "address": address if header.show_address and address else None,
        "phone": phone if header.show_phone and phone else None,
        "ntn": {"label": header.ntn_label, "value": ntn_value} if header.show_ntn and ntn_value else None,
        "extra_lines": [line for line in header.extra_lines if line.strip()],
    }

    # meta rows
    meta: list[dict[str, str]] = []
    if meta_cfg.show_number:
        number = pick_number(sale, meta_cfg.number_source)
        if number:
            meta.append({"key": "number", "label": _label(cfg, "number")["text"], "value": number})
    if meta_cfg.show_datetime:
        meta.append({
            "key": "date",
            "label": _label(cfg, "date")["text"],
            "value": sale.sold_at.astimezone(zone).strftime("%d-%b-%Y %I:%M %p"),
        })
    if meta_cfg.show_cashier and not public and sale.cashier_name:
        meta.append({"key": "cashier", "label": _label(cfg, "cashier")["text"], "value": sale.cashier_name})
    if meta_cfg.show_branch and sale.branch_name:
        meta.append({"key": "branch", "label": _label(cfg, "branch")["text"], "value": sale.branch_name})
    if meta_cfg.show_register and sale.register_name:
        meta.append({"key": "register", "label": _label(cfg, "register")["text"], "value": sale.register_name})
    if meta_cfg.show_customer and not public:
        if sale.customer is not None:
            meta.append({"key": "customer", "label": _label(cfg, "customer")["text"], "value": sale.customer.name})
            if meta_cfg.show_customer_phone and sale.customer.phone:
                meta.append({
                    "key": "customer_phone",
                    "label": _label(cfg, "customer_phone")["text"],
                    "value": sale.customer.phone,
                })
        elif meta_cfg.walkin_label.strip():
            meta.append({"key": "customer", "label": _label(cfg, "customer")["text"],
                         "value": meta_cfg.walkin_label.strip()})

    # items
    items: list[dict[str, Any]] = []
    for ln in sale.lines:
        primary = ln.product_name
        secondary = (ln.product_name_secondary or "").strip() or None
        if items_cfg.name_language == "secondary":
            name, second = (secondary or primary), None
        elif items_cfg.name_language == "both":
            name, second = primary, secondary
        else:
            name, second = primary, None
        if items_cfg.show_variation and ln.variation_name:
            name = f"{name} ({ln.variation_name})"
        if not items_cfg.show_name:
            name, second = "", None
        batches = []
        if items_cfg.show_batch or items_cfg.show_expiry:
            for b in ln.batches:
                entry: dict[str, Any] = {}
                if items_cfg.show_batch and b.batch_no:
                    entry["batch_no"] = b.batch_no
                if items_cfg.show_expiry and b.expiry_date:
                    entry["expiry"] = _expiry_text(b.expiry_date)
                if entry:
                    if len(ln.batches) > 1:
                        entry["qty"] = qty_text(b.qty, ln.unit_allows_decimal)
                    batches.append(entry)
        items.append({
            "name": name,
            "name_secondary": second,
            "sku": ln.sku if items_cfg.show_sku and ln.sku else None,
            "barcode": ln.barcode if items_cfg.show_barcode and ln.barcode else None,
            "unit": ln.unit if items_cfg.show_unit and ln.unit else None,
            "qty": qty_text(ln.qty, ln.unit_allows_decimal) if items_cfg.show_qty else None,
            "unit_price": money(ln.unit_price) if items_cfg.show_unit_price else None,
            "discount": money(ln.discount_amount)
            if items_cfg.show_line_discount and ln.discount_amount else None,
            "line_total": money(ln.line_total) if items_cfg.show_line_total else None,
            "batches": batches,
        })
    columns = [{"key": "item", "label": _label(cfg, "item")["text"]}]
    if items_cfg.show_qty:
        columns.append({"key": "qty", "label": _label(cfg, "qty")["text"]})
    if items_cfg.show_unit_price:
        columns.append({"key": "price", "label": _label(cfg, "price")["text"]})
    if items_cfg.show_line_total:
        columns.append({"key": "amount", "label": _label(cfg, "amount")["text"]})

    # totals: straight from the sale snapshot
    totals: list[dict[str, Any]] = []
    notes: list[str] = []
    if totals_cfg.show_subtotal:
        totals.append({"key": "subtotal", "label": _label(cfg, "subtotal")["text"], "value": money(s.subtotal)})
    if totals_cfg.show_discount and s.total_discount:
        totals.append({"key": "discount", "label": _label(cfg, "discount")["text"],
                       "value": "-" + money(s.total_discount)})
    if totals_cfg.show_tax_breakdown and s.show_tax_breakdown:
        if s.tax_inclusive:
            totals.append({"key": "net_amount", "label": _label(cfg, "net_amount")["text"],
                           "value": money(s.net_amount)})
        for item in s.tax_breakdown:
            totals.append({
                "key": "tax",
                "label": f"{item.tax_label} {_rate_text(item.tax_rate)}%",
                "value": money(item.tax_amount),
            })
    if totals_cfg.show_rounding and s.rounding_amount:
        sign = "+" if s.rounding_amount > 0 else "-"
        totals.append({"key": "rounding", "label": _label(cfg, "rounding")["text"],
                       "value": sign + money(abs(s.rounding_amount))})
    totals.append({
        "key": "total",
        "label": f"{_label(cfg, 'total')['text']} ({sale.business.currency or 'PKR'})",
        "value": money(s.total_amount),
        "strong": True,
    })
    if totals_cfg.show_includes_tax_note and s.tax_inclusive and s.total_tax:
        notes.append(_label(cfg, "includes_tax")["text"])

    payments: list[dict[str, str]] = []
    if totals_cfg.show_payments:
        for p in sale.payments:
            method = str(p.payment_method)
            label = _label(cfg, method)["text"] if method in LABEL_DEFAULTS else method.replace("_", " ").title()
            payments.append({"key": method, "label": label, "value": money(p.amount)})
        payments.append({"key": "paid", "label": _label(cfg, "paid")["text"], "value": money(s.total_paid)})
    if s.balance_due:
        payments.append({"key": "balance_due", "label": _label(cfg, "balance_due")["text"],
                         "value": money(s.balance_due)})
    if totals_cfg.show_change and s.change_given:
        payments.append({"key": "change", "label": _label(cfg, "change")["text"], "value": money(s.change_given)})
    if not public and sale.customer is not None:
        if totals_cfg.show_previous_balance and sale.customer_previous_balance is not None:
            payments.append({"key": "previous_balance", "label": _label(cfg, "previous_balance")["text"],
                             "value": money(sale.customer_previous_balance)})
        if totals_cfg.show_balance_after and sale.customer_balance_after is not None:
            payments.append({"key": "balance_after", "label": _label(cfg, "balance_after")["text"],
                             "value": money(sale.customer_balance_after)})

    # footer
    business_footer = (sale.receipt_footer or "").strip()
    custom = bool(footer_cfg.text_en.strip() or business_footer)
    footer_lines = []
    en = footer_cfg.text_en.strip() or business_footer or _label(cfg, "thank_you")["en"]
    ur = footer_cfg.text_ur.strip() or ("" if custom else _label(cfg, "thank_you")["ur"])
    footer_lines.append(en)
    if ur:
        footer_lines.append(ur)

    # QR: the public receipt link, only for a synced sale, never on the page it points to
    qr = None
    if footer_cfg.show_qr and sale.synced and qr_url and channel == "print":
        qr = {"content": qr_url}

    is_void = str(sale.status).lower() in ("voided", "void", "cancelled", "canceled")
    if layout.design == "a4_tax_invoice":
        title = _label(cfg, "tax_invoice")
    elif layout.design == "a4_elegant":
        title = _label(cfg, "invoice")
    else:
        title = _label(cfg, "receipt")

    return {
        "v": MODEL_VERSION,
        "channel": channel,
        "layout": {"id": layout.id, "name": layout.name, "design": layout.design, "paper": layout.paper},
        "paper": {
            "code": layout.paper,
            "width_mm": PAPER_WIDTH_MM[layout.paper],
            "a4": layout.paper == "a4",
        },
        "style": dict(cfg["style"]),
        "currency": sale.business.currency or "PKR",
        "is_void": is_void,
        "void_label": _label(cfg, "void")["text"] if is_void else None,
        "title": title["text"],
        "header": header_model,
        "meta": meta,
        "columns": columns,
        "items": items,
        "totals": totals,
        "payments": payments,
        "notes": notes,
        "footer": {
            "lines": footer_lines,
            "powered_by": _label(cfg, "powered_by")["text"] if footer_cfg.show_powered_by else None,
        },
        "qr": qr,
        "link": qr_url if (footer_cfg.show_qr and sale.synced) else None,
        "show_dividers": bool(cfg["style"]["show_dividers"]),
    }


# ── HTML ─────────────────────────────────────────────────────────────────────

_FONTS = "'Noto Naskh Arabic','Noto Nastaliq Urdu','Segoe UI',Tahoma,-apple-system,Roboto,Arial,sans-serif"
_PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f2f2f2;font-family:%(fonts)s;color:#111}
.r{margin:12px auto;background:#fff;padding:18px 16px;border-radius:8px;font-size:%(size)spx;line-height:1.45}
.r.roll58{max-width:260px}.r.roll80{max-width:360px}.r.a4{max-width:794px;padding:36px 40px}
h1{font-size:1.35em;margin:0 0 2px;text-align:center}
.r.a4 h1{text-align:left}
.c{text-align:center;color:#555;font-size:.87em}
.r.a4 .c{text-align:left}
.v{display:inline-block;margin:8px 0;padding:2px 10px;border:2px solid #b00020;color:#b00020;font-weight:700}
hr{border:0;border-top:1px dashed #bbb;margin:10px 0}
.nodiv hr{border:0;margin:6px 0}
table{width:100%%;border-collapse:collapse}
td,th{padding:3px 0;vertical-align:top}
th{text-align:left;font-size:.8em;color:#666;font-weight:600}
.n{text-align:right;white-space:nowrap;padding-left:8px}
.t td{font-weight:700;font-size:1.15em;padding-top:6px}
.m{color:#666;font-size:.8em}
.f{margin-top:12px;text-align:center;color:#444;white-space:pre-line}
.q{text-align:center;margin-top:10px;word-break:break-all;font-size:.75em;color:#666}
.compact td{padding:1px 0}
.a4 table.items{margin-top:8px}.a4 table.items th{border-bottom:2px solid #333;padding:6px 4px;color:#333}
.a4 table.items td{border-bottom:1px solid #e3e3e3;padding:6px 4px}
.elegant .banner{background:#1f3a5f;color:#fff;padding:14px 18px;border-radius:6px;margin-bottom:12px}
.elegant .banner h1,.elegant .banner .c{color:#fff}
.elegant table.items th{background:#eef2f7;border-bottom:0}
.title{font-weight:700;text-align:center;margin:6px 0;letter-spacing:.04em}
.r.a4 .title{text-align:left;font-size:1.4em}
.pay{margin-top:6px}
"""


def _span(text: str) -> str:
    # dir=auto lets Urdu runs lay out right-to-left inside an otherwise left-to-right page
    return f"<span dir='auto'>{escape(text)}</span>" if text else ""


def render_html(model: dict[str, Any], *, pdf_href: str | None = None) -> str:
    e = escape
    paper = model["paper"]["code"]
    scale = float(model["style"].get("font_scale", 1.0))
    base = 14 if paper != "roll58" else 12.5
    size = f"{base * scale:.1f}"
    classes = ["r", paper]
    if model["layout"]["design"] == "a4_elegant":
        classes.append("elegant")
    if not model["show_dividers"]:
        classes.append("nodiv")
    if model["style"].get("compact_items"):
        classes.append("compact")
    hr = "<hr>"
    h = model["header"]

    head = ""
    if h["logo_url"]:
        head += f"<div class='c'><img src='{e(h['logo_url'])}' alt='' style='max-height:56px;max-width:60%'></div>"
    if h["business_name"]:
        head += f"<h1>{_span(h['business_name'])}</h1>"
    if h["address"]:
        head += f"<div class='c'>{_span(h['address'])}</div>"
    if h["phone"]:
        head += f"<div class='c'>{e(h['phone'])}</div>"
    if h["ntn"]:
        head += f"<div class='c'>{e(h['ntn']['label'])}: {e(h['ntn']['value'])}</div>"
    for line in h["extra_lines"]:
        head += f"<div class='c'>{_span(line)}</div>"
    if model["layout"]["design"] == "a4_elegant":
        head = f"<div class='banner'>{head}</div>"
    head += f"<div class='title'>{_span(model['title'])}</div>"
    if model["is_void"]:
        head += f"<div class='c'><span class='v'>{_span(model['void_label'])}</span></div>"

    meta = "".join(
        f"<tr><td class='m'>{_span(m['label'])}</td><td class='n'>{_span(m['value'])}</td></tr>"
        for m in model["meta"]
    )
    meta_html = f"{hr}<table>{meta}</table>" if meta else ""

    cols = model["columns"]
    a4 = model["paper"]["a4"]
    rows = []
    if a4:
        header_cells = "".join(
            f"<th{' class=n' if c['key'] != 'item' else ''}>{_span(c['label'])}</th>" for c in cols
        )
        for it in model["items"]:
            name = _span(it["name"])
            if it["name_secondary"]:
                name += f"<div class='m'>{_span(it['name_secondary'])}</div>"
            extra = _item_extra(it)
            if extra:
                name += f"<div class='m'>{_span(extra)}</div>"
            cells = [f"<td>{name}</td>"]
            for c in cols[1:]:
                if c["key"] == "qty":
                    cells.append(f"<td class='n'>{e((it['qty'] or '') + (' ' + it['unit'] if it['unit'] else ''))}</td>")
                elif c["key"] == "price":
                    cells.append(f"<td class='n'>{e(it['unit_price'] or '')}</td>")
                else:
                    cells.append(f"<td class='n'>{e(it['line_total'] or '')}</td>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        table = f"<table class='items'><tr>{header_cells}</tr>{''.join(rows)}</table>"
    else:
        for it in model["items"]:
            name = _span(it["name"])
            if it["name_secondary"]:
                name += f"<div class='m'>{_span(it['name_secondary'])}</div>"
            detail = _item_detail(it)
            if detail:
                name += f"<div class='m'>{e(detail)}</div>"
            extra = _item_extra(it)
            if extra:
                name += f"<div class='m'>{_span(extra)}</div>"
            amount = f"<td class='n'>{e(it['line_total'])}</td>" if it["line_total"] is not None else "<td></td>"
            rows.append(f"<tr><td>{name}</td>{amount}</tr>")
        table = (
            f"<table><tr><th>{_span(cols[0]['label'])}</th>"
            f"<th class='n'>{e(model['currency'])}</th></tr>{''.join(rows)}</table>"
        )

    totals = "".join(
        f"<tr{' class=t' if t.get('strong') else ''}><td>{_span(t['label'])}</td><td class='n'>{e(t['value'])}</td></tr>"
        for t in model["totals"]
    )
    pays = "".join(
        f"<tr><td>{_span(p['label'])}</td><td class='n'>{e(p['value'])}</td></tr>" for p in model["payments"]
    )
    notes = "".join(f"<div class='c m'>{_span(n)}</div>" for n in model["notes"])
    footer = "".join(f"<div>{_span(line)}</div>" for line in model["footer"]["lines"] if line)
    if model["footer"]["powered_by"]:
        footer += f"<div class='m'>{_span(model['footer']['powered_by'])}</div>"
    qr_html = (
        f"<div class='q'>{_qr_svg(model['qr']['content'])}<div>{e(model['qr']['content'])}</div></div>"
        if model["qr"] else ""
    )
    pdf = f"<p class='c'><a href='{e(pdf_href)}'>Download PDF</a></p>" if pdf_href else ""
    number = next((m["value"] for m in model["meta"] if m["key"] == "number"), "")
    title = f"Receipt {number}".strip()
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        f"<title>{e(title)}</title><style>{_PAGE_CSS % {'fonts': _FONTS, 'size': size}}</style></head><body>"
        f"<div class='{' '.join(classes)}'>{head}{meta_html}{hr}{table}{hr}<table>{totals}</table>{notes}"
        + (f"{hr}<table class='pay'>{pays}</table>" if pays else "")
        + f"<div class='f'>{footer}</div>{qr_html}{pdf}</div></body></html>"
    )


def _item_detail(it: dict[str, Any]) -> str:
    parts = []
    qty = it["qty"]
    if qty is not None:
        qty_part = qty + (f" {it['unit']}" if it["unit"] else "")
        parts.append(qty_part + (f" x {it['unit_price']}" if it["unit_price"] is not None else ""))
    elif it["unit_price"] is not None:
        parts.append(it["unit_price"])
    if it["discount"]:
        parts.append(f"-{it['discount']}")
    return " ".join(parts)


def _item_extra(it: dict[str, Any]) -> str:
    """SKU / barcode / batch-and-expiry caption lines (joined for the HTML renderers)."""
    return " | ".join(item_extra_lines(it))


_BATCH_LABELS = {"batch": "Batch", "expiry": "Exp"}


def item_extra_lines(it: dict[str, Any]) -> list[str]:
    lines = []
    ids = [x for x in (f"SKU {it['sku']}" if it["sku"] else None, it["barcode"]) if x]
    if ids:
        lines.append(" ".join(ids))
    for b in it["batches"]:
        bits = []
        if b.get("batch_no"):
            bits.append(f"{_BATCH_LABELS['batch']}: {b['batch_no']}")
        if b.get("expiry"):
            bits.append(f"{_BATCH_LABELS['expiry']}: {b['expiry']}")
        if b.get("qty") and len(it["batches"]) > 1:
            bits.append(f"x{b['qty']}")
        if bits:
            lines.append(" ".join(bits))
    return lines


def _qr_svg(content: str) -> str:
    """Inline SVG QR code (no remote loads: the receipt page has a strict CSP)."""
    try:
        import segno
    except ImportError:  # optional dependency: the model still carries qr.content
        return ""
    return segno.make(content, error="m").svg_inline(scale=3, border=1, dark="#000", light="#fff")


def error_page(message: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'><title>Receipt</title>"
        f"<style>{_PAGE_CSS % {'fonts': _FONTS, 'size': '14'}}</style></head><body><div class='r roll80'>"
        f"<h1>Receipt unavailable</h1><p class='c'>{escape(message)}</p></div></body></html>"
    )


# ── plain text (WhatsApp) ────────────────────────────────────────────────────


def render_text(model: dict[str, Any]) -> str:
    out: list[str] = []
    h = model["header"]
    if h["business_name"]:
        out.append(f"*{h['business_name']}*")
    for value in (h["address"], h["phone"]):
        if value:
            out.append(value)
    if h["ntn"]:
        out.append(f"{h['ntn']['label']}: {h['ntn']['value']}")
    out.extend(h["extra_lines"])
    out.append(f"*{model['title']}*" + (f" {model['void_label']}" if model["is_void"] else ""))
    for m in model["meta"]:
        out.append(f"{m['label']}: {m['value']}")
    out.append("--------------------")
    for index, it in enumerate(model["items"], 1):
        out.append(f"{index}. {it['name']}".rstrip())
        if it["name_secondary"]:
            out.append(f"   {it['name_secondary']}")
        detail = _item_detail(it)
        if it["line_total"] is not None:
            detail = (detail + " = " if detail else "") + it["line_total"]
        if detail:
            out.append(f"   {detail}")
        for line in item_extra_lines(it):
            out.append(f"   {line}")
    out.append("--------------------")
    for t in model["totals"]:
        text = f"{t['label']}: {t['value']}"
        out.append(f"*{text}*" if t.get("strong") else text)
    out.extend(model["notes"])
    for p in model["payments"]:
        out.append(f"{p['label']}: {p['value']}")
    out.append("--------------------")
    out.extend(line for line in model["footer"]["lines"] if line)
    if model["footer"]["powered_by"]:
        out.append(model["footer"]["powered_by"])
    if model["link"]:
        out.append(model["link"])
    return "\n".join(out)


# ── PDF ──────────────────────────────────────────────────────────────────────

_FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"


def _shape(text: str) -> str:
    if not text or not _URDU.search(text):
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(text))


def _new_pdf(width: float, height: float | None, a4: bool):
    from fpdf import FPDF

    pdf = FPDF(unit="mm", format="A4" if a4 else (width, height or 2000.0))
    sans = _FONT_DIR / "NotoSans-Regular.ttf"
    naskh = _FONT_DIR / "NotoNaskhArabic-Regular.ttf"
    if sans.exists():
        pdf.add_font("NotoSans", "", str(sans))
        pdf.add_font("NotoSans", "B", str(sans))
        if naskh.exists():
            pdf.add_font("NotoArabic", "", str(naskh))
            pdf.add_font("NotoArabic", "B", str(naskh))
            pdf.set_fallback_fonts(["NotoArabic"])
        pdf._receipt_font = "NotoSans"  # noqa: SLF001
    else:  # pragma: no cover - fonts ship with the repo
        pdf._receipt_font = "Helvetica"  # noqa: SLF001
    margin = 15.0 if a4 else (3.0 if width <= 58 else 4.0)
    pdf.set_margins(margin, margin, margin)
    pdf.set_auto_page_break(auto=a4, margin=margin)
    pdf.add_page()
    return pdf, margin


def _draw_pdf(pdf, margin: float, model: dict[str, Any]) -> None:
    a4 = model["paper"]["a4"]
    scale = float(model["style"].get("font_scale", 1.0))
    base = 10.0 if a4 else (7.5 if model["paper"]["code"] == "roll58" else 8.5)
    font = pdf._receipt_font  # noqa: SLF001
    inner = pdf.w - 2 * margin
    lh = base * 0.45 * scale + 0.6
    compact = bool(model["style"].get("compact_items"))

    def setf(size: float = base, bold: bool = False) -> None:
        pdf.set_font(font, "B" if bold else "", size * scale)

    def line_h(size: float = base) -> float:
        return size * scale * 0.42 + 0.9

    def centered(text: str, size: float = base, bold: bool = False) -> None:
        setf(size, bold)
        pdf.multi_cell(inner, line_h(size), _shape(text), align="L" if a4 else "C",
                       new_x="LMARGIN", new_y="NEXT")

    def rule() -> None:
        if not model["show_dividers"]:
            pdf.ln(1.5)
            return
        y = pdf.get_y() + 0.8
        pdf.set_draw_color(150, 150, 150)
        pdf.line(margin, y, pdf.w - margin, y)
        pdf.ln(2.2)

    def pair(left: str, right: str, bold: bool = False, size: float = base, right_w: float | None = None) -> None:
        setf(size, bold)
        rw = right_w or min(inner * 0.38, max(18.0, pdf.get_string_width(right) + 2))
        lw = inner - rw
        x0, y0 = pdf.l_margin, pdf.get_y()
        pdf.multi_cell(lw, line_h(size), _shape(left), new_x="LMARGIN", new_y="NEXT")
        y1 = pdf.get_y()
        pdf.set_xy(x0 + lw, y0)
        pdf.cell(rw, line_h(size), right, align="R")
        pdf.set_y(max(y1, y0 + line_h(size)))

    h = model["header"]
    if h["business_name"]:
        centered(h["business_name"], base * 1.35, True)
    for value in (h["address"], h["phone"]):
        if value:
            centered(value, base * 0.9)
    if h["ntn"]:
        centered(f"{h['ntn']['label']}: {h['ntn']['value']}", base * 0.9)
    for line in h["extra_lines"]:
        centered(line, base * 0.9)
    centered(model["title"], base * (1.3 if a4 else 1.0), True)
    if model["is_void"]:
        centered(f"*** {model['void_label']} ***", base * 1.2, True)
    rule()
    for m in model["meta"]:
        pair(m["label"], m["value"], size=base * 0.9)
    rule()

    if a4:
        cols = model["columns"]
        weights = {"item": 0.46, "qty": 0.14, "price": 0.18, "amount": 0.22}
        total_w = sum(weights[c["key"]] for c in cols)
        widths = [inner * weights[c["key"]] / total_w for c in cols]
        setf(base, True)
        for c, w in zip(cols, widths):
            pdf.cell(w, line_h() + 1, _shape(c["label"]), border="B", align="L" if c["key"] == "item" else "R")
        pdf.ln(line_h() + 1.5)
        for it in model["items"]:
            setf(base)
            y0 = pdf.get_y()
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(widths[0], line_h(), _shape(it["name"]), new_x="RIGHT", new_y="TOP")
            y_after = pdf.get_y()
            x = pdf.l_margin + widths[0]
            for c, w in zip(cols[1:], widths[1:]):
                pdf.set_xy(x, y0)
                if c["key"] == "qty":
                    text = (it["qty"] or "") + (f" {it['unit']}" if it["unit"] else "")
                elif c["key"] == "price":
                    text = it["unit_price"] or ""
                else:
                    text = it["line_total"] or ""
                pdf.cell(w, line_h(), text, align="R")
                x += w
            pdf.set_xy(pdf.l_margin, max(y_after, y0 + line_h()))
            extras = ([it["name_secondary"]] if it["name_secondary"] else []) + item_extra_lines(it)
            for extra in extras:
                setf(base * 0.8)
                pdf.set_text_color(100, 100, 100)
                pdf.multi_cell(inner, line_h(base * 0.8), _shape(extra), new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)
    else:
        for it in model["items"]:
            amount = it["line_total"] or ""
            pair(it["name"], amount, bold=False)
            if it["name_secondary"]:
                setf(base * 0.9)
                pdf.multi_cell(inner, line_h(base * 0.9), _shape(it["name_secondary"]),
                               new_x="LMARGIN", new_y="NEXT")
            detail = _item_detail(it)
            if detail:
                setf(base * 0.85)
                pdf.set_text_color(90, 90, 90)
                pdf.multi_cell(inner, line_h(base * 0.85), "  " + detail, new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)
            for extra in item_extra_lines(it):
                setf(base * 0.8)
                pdf.set_text_color(90, 90, 90)
                pdf.multi_cell(inner, line_h(base * 0.8), _shape("  " + extra), new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)
            if not compact:
                pdf.ln(0.6)
    rule()
    for t in model["totals"]:
        pair(t["label"], t["value"], bold=bool(t.get("strong")), size=base * (1.15 if t.get("strong") else 1.0))
    for note in model["notes"]:
        centered(note, base * 0.85)
    if model["payments"]:
        rule()
        for p in model["payments"]:
            pair(p["label"], p["value"])
    rule()
    for line in model["footer"]["lines"]:
        if line:
            centered(line, base * 0.95)
    if model["footer"]["powered_by"]:
        centered(model["footer"]["powered_by"], base * 0.8)
    if model["qr"]:
        try:
            import segno
        except ImportError:  # optional dependency: the model still carries qr.content
            return
        matrix = segno.make(model["qr"]["content"], error="m").matrix
        side = min(inner, 28.0 if not a4 else 32.0)
        cell = side / len(matrix)
        pdf.ln(1.5)
        x0 = pdf.l_margin + ((inner - side) / 2 if not a4 else 0)
        y0 = pdf.get_y()
        pdf.set_fill_color(0, 0, 0)
        for r, row in enumerate(matrix):
            for c, bit in enumerate(row):
                if bit:
                    pdf.rect(x0 + c * cell, y0 + r * cell, cell + 0.02, cell + 0.02, style="F")
        pdf.set_y(y0 + side + 1)


def render_pdf(model: dict[str, Any]) -> bytes:
    """Thermal rolls are rendered at their exact width and (measured) content height; A4 is A4."""
    a4 = model["paper"]["a4"]
    width = model["paper"]["width_mm"]
    if a4:
        pdf, margin = _new_pdf(width, None, True)
        _draw_pdf(pdf, margin, model)
        return bytes(pdf.output())
    probe, margin = _new_pdf(width, 3000.0, False)
    _draw_pdf(probe, margin, model)
    height = round(probe.get_y() + margin + 2.0, 1)
    pdf, margin = _new_pdf(width, height, False)
    pdf.set_auto_page_break(auto=False)
    _draw_pdf(pdf, margin, model)
    return bytes(pdf.output())
