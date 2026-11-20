"""Barcode normalization: one place for lookup, uniqueness checks, imports and Quick Add."""

from __future__ import annotations

import pytest

from app.services.barcode_utils import (
    gtin_check_digit,
    is_valid_gtin,
    match_candidates,
    normalize_barcode,
    same_item,
)

# Real, published codes.
COCA_COLA_EAN13 = "5449000000996"
STABILO_EAN13 = "4006381333931"
PK_896_EAN13 = "8964000370018"  # 896 = GS1 Pakistan prefix
EAN8 = "96385074"
EAN8_B = "73513537"
UPCA = "036000291452"  # Wikipedia's UPC-A example
GTIN14 = "10012345678902"  # case code (indicator 1) of EAN-13 0012345678905 / UPC-A 012345678905


@pytest.mark.parametrize(
    ("code", "fmt"),
    [
        (COCA_COLA_EAN13, "EAN13"),
        (STABILO_EAN13, "EAN13"),
        (PK_896_EAN13, "EAN13"),
        (EAN8, "EAN8"),
        (EAN8_B, "EAN8"),
        (UPCA, "UPCA"),
        (GTIN14, "GTIN14"),
    ],
)
def test_valid_formats(code, fmt):
    n = normalize_barcode(code)
    assert n.format == fmt
    assert n.valid is True
    assert is_valid_gtin(code)


def test_ean13_is_kept_as_is():
    n = normalize_barcode(COCA_COLA_EAN13)
    assert n.canonical == COCA_COLA_EAN13
    assert n.barcode_type == "EAN13"


def test_upca_becomes_ean13_with_leading_zero():
    n = normalize_barcode(UPCA)
    assert n.canonical == "0" + UPCA
    assert len(n.canonical) == 13
    assert n.format == "UPCA"
    assert n.valid
    assert is_valid_gtin(n.canonical)  # the EAN-13 form has the same check digit


def test_gtin14_is_kept_and_its_13_digit_form_is_tried_for_lookup():
    n = normalize_barcode(GTIN14)
    assert n.canonical == GTIN14
    assert "0012345678905" in n.lookup_forms
    assert "012345678905" in n.lookup_forms
    # ...but it is not treated as that item for uniqueness
    assert "0012345678905" not in n.forms


@pytest.mark.parametrize(
    "bad", ["5449000000995", "96385075", "036000291453", "10012345678903", "4006381333932"]
)
def test_bad_check_digit_is_flagged_never_rejected(bad):
    n = normalize_barcode(bad)
    assert n.valid is False
    assert n.digits == bad
    assert n.canonical  # still usable
    assert not n.empty


@pytest.mark.parametrize(
    "messy",
    [
        " 5449000000996 ",
        "5449 0000 00996",
        "5449-0000-00996",
        "\t5449000000996\n",
        "﻿5449000000996",
        "]E05449000000996",  # AIM symbology identifier some scanners prepend
    ],
)
def test_whitespace_and_junk_are_stripped(messy):
    n = normalize_barcode(messy)
    assert n.canonical == COCA_COLA_EAN13
    assert n.valid


def test_arabic_indic_digits():
    n = normalize_barcode("٥٤٤٩٠٠٠٠٠٠٩٩٦")
    assert n.canonical == COCA_COLA_EAN13


def test_empty_and_garbage():
    assert normalize_barcode("").empty
    assert normalize_barcode("   ").empty
    assert normalize_barcode(None).empty  # type: ignore[arg-type]
    assert normalize_barcode("").valid is False


def test_unknown_length_is_not_rejected():
    n = normalize_barcode("1234567")
    assert n.canonical == "1234567"
    assert n.format == "UNKNOWN"
    assert n.valid is False
    assert n.barcode_type == "OTHER"


def test_custom_alphanumeric_codes_never_cross_match_by_digits():
    n = normalize_barcode("AB-123")
    assert n.custom is True
    assert n.canonical == "AB-123"
    assert n.forms == ()
    assert list(match_candidates("AB-123")) == ["AB-123"]
    assert not same_item("AB-123", "123")


def test_forms_cross_match_upca_ean13_and_gtin14_padding():
    assert same_item(UPCA, "0" + UPCA)
    assert same_item(UPCA, "00" + UPCA)
    assert same_item("0" + UPCA, UPCA)
    assert same_item(COCA_COLA_EAN13, "0" + COCA_COLA_EAN13)  # GTIN-14 zero-padded form
    assert not same_item(COCA_COLA_EAN13, STABILO_EAN13)


def test_ean8_zero_pads_to_longer_forms():
    assert same_item(EAN8, "0000" + EAN8)
    assert not same_item(EAN8, "000" + EAN8)  # 11 digits is not a GTIN width: no false match


def test_odd_width_digit_codes_do_not_pad():
    assert not same_item("12345", "0012345")


def test_match_candidates_put_the_typed_spelling_first():
    got = match_candidates(" " + UPCA + " ")
    assert got[0] == UPCA
    assert "0" + UPCA in got
    assert len(got) == len(set(got))


def test_match_candidates_lookup_adds_the_gtin14_base_forms():
    plain = match_candidates(GTIN14)
    with_lookup = match_candidates(GTIN14, lookup=True)
    assert "0012345678905" not in plain
    assert "0012345678905" in with_lookup
    assert with_lookup[0] == GTIN14


def test_check_digit_algorithm():
    assert int(gtin_check_digit("544900000099")) == 6
    assert int(gtin_check_digit("03600029145")) == 2
    assert int(gtin_check_digit("9638507")) == 4
