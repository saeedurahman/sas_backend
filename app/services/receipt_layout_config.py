"""Receipt layout configuration: the validated, versioned JSON a layout is made of.

A layout is business data, not code. ``LayoutConfig`` is the schema (unknown keys are
rejected, every missing key gets its default), ``DESIGNS`` describes the four built-in
designs and ``default_config`` builds the starting config of a design (optionally with the
pharmacy preset that prints batch and expiry).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DESIGNS = ("thermal_classic", "thermal_compact", "a4_tax_invoice", "a4_elegant")
PAPERS = ("roll58", "roll80", "a4")
CHANNELS = ("print", "whatsapp", "link")

# design -> default paper
DESIGN_PAPER = {
    "thermal_classic": "roll80",
    "thermal_compact": "roll58",
    "a4_tax_invoice": "a4",
    "a4_elegant": "a4",
}
DESIGN_INFO = {
    "thermal_classic": {
        "title": "Classic receipt",
        "description": "Centered header, one line per item with quantity x price, simple totals.",
        "papers": ["roll80", "roll58"],
    },
    "thermal_compact": {
        "title": "Compact receipt",
        "description": "Tight spacing for 58 mm rolls: item name, then quantity x price and total on one line.",
        "papers": ["roll58", "roll80"],
    },
    "a4_tax_invoice": {
        "title": "A4 tax invoice",
        "description": "Full-page invoice with an item table and tax summary, for customers who need a tax invoice.",
        "papers": ["a4"],
    },
    "a4_elegant": {
        "title": "A4 elegant invoice",
        "description": "Full-page invoice with a banner header and shaded table.",
        "papers": ["a4"],
    },
}

# Every printed caption: key -> (English, Urdu).
LABEL_DEFAULTS: dict[str, tuple[str, str]] = {
    "receipt": ("Receipt", "رسید"),
    "tax_invoice": ("Tax Invoice", "ٹیکس انوائس"),
    "invoice": ("Invoice", "انوائس"),
    "number": ("Receipt #", "رسید نمبر"),
    "date": ("Date", "تاریخ"),
    "cashier": ("Cashier", "کیشیئر"),
    "customer": ("Customer", "گاہک"),
    "customer_phone": ("Phone", "فون"),
    "branch": ("Branch", "برانچ"),
    "register": ("Register", "کاؤنٹر"),
    "item": ("Item", "آئٹم"),
    "qty": ("Qty", "تعداد"),
    "price": ("Price", "قیمت"),
    "amount": ("Amount", "رقم"),
    "sku": ("SKU", "کوڈ"),
    "barcode": ("Barcode", "بار کوڈ"),
    "batch": ("Batch", "بیچ"),
    "expiry": ("Exp", "ایکسپائری"),
    "subtotal": ("Subtotal", "ذیلی کل"),
    "discount": ("Discount", "رعایت"),
    "net_amount": ("Net amount", "خالص رقم"),
    "tax": ("Tax", "ٹیکس"),
    "rounding": ("Rounding", "راؤنڈنگ"),
    "total": ("Total", "کل"),
    "paid": ("Paid", "ادا شدہ"),
    "balance_due": ("Balance due", "بقایا"),
    "change": ("Change", "واپسی"),
    "cash": ("Cash", "نقد"),
    "card": ("Card", "کارڈ"),
    "upi": ("UPI", "یو پی آئی"),
    "bank_transfer": ("Bank transfer", "بینک ٹرانسفر"),
    "wallet": ("Mobile wallet", "موبائل والیٹ"),
    "credit": ("Credit", "ادھار"),
    "cheque": ("Cheque", "چیک"),
    "other": ("Other", "دیگر"),
    "previous_balance": ("Previous balance", "پچھلا بقایا"),
    "balance_after": ("Balance now", "موجودہ بقایا"),
    "includes_tax": ("Prices include tax", "قیمتوں میں ٹیکس شامل ہے"),
    "thank_you": ("Thank you for shopping with us!", "خریداری کا شکریہ!"),
    "void": ("VOID", "منسوخ"),
    "powered_by": ("Powered by PakPOS", "PakPOS کے ذریعے"),
}
LABEL_KEYS = frozenset(LABEL_DEFAULTS)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeaderCfg(_Strict):
    show_logo: bool = True
    show_business_name: bool = True
    show_address: bool = True
    show_phone: bool = True
    show_ntn: bool = True
    ntn_label: str = Field(default="NTN", max_length=20)
    extra_lines: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("extra_lines")
    @classmethod
    def _lines(cls, value: list[str]) -> list[str]:
        for line in value:
            if len(line) > 80:
                raise ValueError("each extra line must be at most 80 characters")
        return value


class MetaCfg(_Strict):
    show_number: bool = True
    number_source: Literal["register", "official"] = "register"
    show_datetime: bool = True
    show_cashier: bool = True
    show_branch: bool = False
    show_register: bool = False
    show_customer: bool = True
    show_customer_phone: bool = False
    walkin_label: str = Field(default="Walk-in", max_length=40)


class ItemsCfg(_Strict):
    show_name: bool = True
    show_variation: bool = True
    show_sku: bool = False
    show_barcode: bool = False
    show_unit: bool = False
    show_qty: bool = True
    show_unit_price: bool = True
    show_line_discount: bool = True
    show_line_total: bool = True
    show_batch: bool = False
    show_expiry: bool = False
    name_language: Literal["primary", "secondary", "both"] = "primary"


class TotalsCfg(_Strict):
    show_subtotal: bool = True
    show_discount: bool = True
    show_tax_breakdown: bool = True
    show_rounding: bool = True
    show_includes_tax_note: bool = True
    show_payments: bool = True
    show_change: bool = True
    show_previous_balance: bool = False
    show_balance_after: bool = False


class FooterCfg(_Strict):
    text_en: str = Field(default="", max_length=300)
    text_ur: str = Field(default="", max_length=300)
    show_powered_by: bool = False
    show_qr: bool = True


class LabelCfg(_Strict):
    en: str = Field(default="", max_length=60)
    ur: str = Field(default="", max_length=60)


class StyleCfg(_Strict):
    font_scale: float = Field(default=1.0, ge=0.8, le=1.4)
    show_dividers: bool = True
    compact_items: bool = False


class LayoutConfig(_Strict):
    v: Literal[1] = 1
    header: HeaderCfg = Field(default_factory=HeaderCfg)
    meta: MetaCfg = Field(default_factory=MetaCfg)
    items: ItemsCfg = Field(default_factory=ItemsCfg)
    totals: TotalsCfg = Field(default_factory=TotalsCfg)
    footer: FooterCfg = Field(default_factory=FooterCfg)
    labels: dict[str, LabelCfg] = Field(default_factory=dict)
    style: StyleCfg = Field(default_factory=StyleCfg)

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: dict[str, LabelCfg]) -> dict[str, LabelCfg]:
        unknown = sorted(set(value) - LABEL_KEYS)
        if unknown:
            raise ValueError(f"unknown label key(s): {', '.join(unknown)}")
        return value

    def with_defaults(self) -> "LayoutConfig":
        """Fill every label key (an empty en/ur falls back to the built-in caption)."""
        full: dict[str, LabelCfg] = {}
        for key, (en, ur) in LABEL_DEFAULTS.items():
            given = self.labels.get(key)
            full[key] = LabelCfg(
                en=(given.en if given and given.en else en),
                ur=(given.ur if given and given.ur else ur),
            )
        return self.model_copy(update={"labels": full})


def normalise_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate ``raw`` and return the full config (all defaults, all labels) as plain JSON."""
    cfg = LayoutConfig.model_validate(raw or {}).with_defaults()
    return cfg.model_dump(mode="json")


def default_config(design: str, *, pharmacy: bool = False) -> dict[str, Any]:
    """Starting config of a design (full JSON, version 1)."""
    if design not in DESIGNS:
        raise ValueError(f"unknown design {design!r}")
    raw: dict[str, Any] = {"v": 1}
    if design == "thermal_compact":
        raw["style"] = {"compact_items": True, "font_scale": 0.9}
        raw["meta"] = {"show_cashier": False, "show_customer": False}
        raw["footer"] = {"show_qr": False}
    elif design == "a4_tax_invoice":
        raw["meta"] = {"show_branch": True, "show_customer_phone": True}
        raw["items"] = {"show_sku": True, "show_unit": True}
        raw["totals"] = {"show_previous_balance": True, "show_balance_after": True}
        raw["header"] = {"extra_lines": []}
    elif design == "a4_elegant":
        raw["meta"] = {"show_branch": True, "show_customer_phone": True}
        raw["items"] = {"show_unit": True}
        raw["style"] = {"show_dividers": False}
    if pharmacy:
        items = dict(raw.get("items", {}))
        items.update({"show_batch": True, "show_expiry": True})
        raw["items"] = items
    return normalise_config(raw)


DESIGN_NAMES = {
    "thermal_classic": "Classic receipt",
    "thermal_compact": "Compact receipt",
    "a4_tax_invoice": "A4 tax invoice",
    "a4_elegant": "A4 elegant invoice",
}


def designs_description() -> list[dict[str, Any]]:
    return [
        {
            "design": design,
            "title": info["title"],
            "description": info["description"],
            "default_paper": DESIGN_PAPER[design],
            "papers": info["papers"],
            "default_config": default_config(design),
            "pharmacy_config": default_config(design, pharmacy=True),
        }
        for design, info in DESIGN_INFO.items()
    ]
