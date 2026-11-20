"""Barcode normalization: the ONE place that decides what a scanned/typed code means.

Used by the product barcode lookup, barcode uniqueness checks, the CSV/price-list imports and
Quick Add. Nothing here touches the database.

Rules
-----
* ``digits``: ASCII digits only. A scanner symbology prefix (AIM id such as ``]E0``), spaces,
  dashes, control characters and any other junk are dropped; Arabic-Indic / Extended Arabic-Indic
  digits are converted to ASCII.
* Format by length: 8 = EAN-8, 12 = UPC-A, 13 = EAN-13, 14 = GTIN-14. The GS1 check digit is
  validated for those lengths and reported in ``valid``. **Nothing is ever rejected**: other
  lengths (internal / in-store codes) and bad check digits are kept as they are with
  ``valid = False``.
* ``canonical`` is the form we store for new barcodes: UPC-A (12) becomes EAN-13 with a leading
  ``0``; everything else is the digits as scanned.
* ``forms`` are all spellings of the SAME trade item, used to match data that was saved in
  another form: GTIN-8/12/13/14 are zero-padded views of one number, so ``012345678905`` (UPC-A),
  ``0012345678905`` (EAN-13) and ``00012345678905`` (GTIN-14) match each other, as do an EAN-8 and
  its zero-padded longer forms. Uniqueness checks and imports use ``forms``.
* ``lookup_forms`` = ``forms`` plus, for a GTIN-14 whose indicator digit is not ``0`` (a case /
  inner pack), the 13-digit form of the base item (indicator dropped, check digit recomputed).
  Only the scan **lookup** uses it, as a fallback suggestion: a case code is not the same item,
  so it never counts as a duplicate and Quick Add never adds stock through it.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

GTIN_LENGTHS = (8, 12, 13, 14)
_FORMAT_BY_LENGTH = {8: "EAN8", 12: "UPCA", 13: "EAN13", 14: "GTIN14"}


@dataclass(frozen=True)
class NormalizedBarcode:
    raw: str
    digits: str
    canonical: str
    format: str  # EAN8 | UPCA | EAN13 | GTIN14 | UNKNOWN | EMPTY
    valid: bool
    forms: tuple[str, ...]
    lookup_forms: tuple[str, ...]
    # Contains letters: an in-store / custom code. Kept verbatim, never cross-matched by digits.
    custom: bool = False

    @property
    def empty(self) -> bool:
        return not self.digits

    @property
    def barcode_type(self) -> str:
        """Value for ``barcodes.barcode_type`` (String(20))."""
        return self.format if self.format in _FORMAT_BY_LENGTH.values() else "OTHER"


def gtin_check_digit(body: str) -> int:
    """GS1 check digit for ``body`` (the GTIN without its last digit)."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10


def is_valid_gtin(digits: str) -> bool:
    """True when ``digits`` is 8/12/13/14 digits with a correct GS1 check digit."""
    return (
        len(digits) in GTIN_LENGTHS
        and digits.isascii()
        and digits.isdigit()
        and gtin_check_digit(digits[:-1]) == int(digits[-1])
    )


def _digits_only(raw: str) -> str:
    text = (raw or "").strip()
    # AIM symbology identifier some scanners prepend: "]" + code char + modifier, e.g. "]E0".
    if len(text) >= 4 and text[0] == "]" and text[1].isalpha():
        text = text[3:]
    out: list[str] = []
    for ch in text:
        if "0" <= ch <= "9":
            out.append(ch)
        elif ch.isdecimal():  # Arabic-Indic and friends
            out.append(str(unicodedata.decimal(ch)))
    return "".join(out)


def _unique(items: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item not in seen:
            seen[item] = None
    return tuple(seen)


def normalize_barcode(raw: str | None) -> NormalizedBarcode:
    digits = _digits_only(raw or "")
    if not digits:
        return NormalizedBarcode(raw or "", "", "", "EMPTY", False, (), ())

    typed = (raw or "").strip()
    if any(ch.isalpha() for ch in typed) and not (len(typed) >= 4 and typed[0] == "]"):
        # "AB-123" is a custom code; its digits ("123") say nothing about another product's "123".
        return NormalizedBarcode(raw or "", digits, typed, "UNKNOWN", False, (), (), custom=True)

    fmt = _FORMAT_BY_LENGTH.get(len(digits), "UNKNOWN")
    valid = is_valid_gtin(digits)
    canonical = "0" + digits if fmt == "UPCA" else digits

    forms: list[str] = [digits, canonical]
    if len(digits) in GTIN_LENGTHS:
        stripped = digits.lstrip("0")
        for length in GTIN_LENGTHS:
            if len(stripped) <= length:
                forms.append(stripped.zfill(length))
    lookup = list(forms)

    if len(digits) == 14 and digits[0] != "0":
        base = digits[1:13]
        base13 = base + str(gtin_check_digit(base))
        lookup.append(base13)
        if base13.startswith("0"):
            lookup.append(base13[1:])

    return NormalizedBarcode(
        raw=raw or "",
        digits=digits,
        canonical=canonical,
        format=fmt,
        valid=valid,
        forms=_unique(forms),
        lookup_forms=_unique(lookup),
    )


def barcode_forms(raw: str | None) -> tuple[str, ...]:
    """Every spelling of the same item (uniqueness / import matching). Empty when no digits."""
    return normalize_barcode(raw).forms


def barcode_lookup_forms(raw: str | None) -> tuple[str, ...]:
    """``barcode_forms`` plus the GTIN-14 base-item fallback (scan lookup only)."""
    return normalize_barcode(raw).lookup_forms


def match_candidates(raw: str | None, *, lookup: bool = False) -> tuple[str, ...]:
    """Strings to compare against stored ``barcodes.barcode`` values.

    The input exactly as typed (trimmed) comes first, so custom non-numeric codes such as
    ``"AB-123"`` keep matching themselves, then every equivalent numeric spelling.
    ``lookup=True`` adds the GTIN-14 base-item fallback.
    """
    n = normalize_barcode(raw)
    typed = (raw or "").strip()
    return _unique([typed, *(n.lookup_forms if lookup else n.forms)])


def same_item(a: str | None, b: str | None) -> bool:
    """True when two codes are spellings of the same trade item."""
    fa, fb = barcode_forms(a), barcode_forms(b)
    return bool(fa) and bool(fb) and bool(set(fa) & set(fb))
