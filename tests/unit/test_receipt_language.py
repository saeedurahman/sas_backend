"""Receipt language modes (en / ur / both), RTL, config v2 upgrade and legacy-option mapping. No server."""

from __future__ import annotations

import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.schemas.invoice import CustomerInfoForInvoice
from app.services.receipt_layout_config import (
    DESIGN_PAPER,
    DESIGNS,
    LABEL_DEFAULTS,
    default_config,
    normalise_config,
    upgrade_config,
)
from app.services.receipt_layout_service import SAMPLE_LINK, mock_sale
from app.services.receipt_legacy import LegacyOptions, apply_legacy, read_legacy
from app.services.receipt_render import (
    LayoutView,
    build_receipt_model,
    render_html,
    render_pdf,
    render_text,
)

ZONE = ZoneInfo("Asia/Karachi")
BIZ = SimpleNamespace(
    name="Al Shifa Pharmacy", legal_name=None, phone="0300-1", tax_id="1234567-8",
    address_line1="Main Bazaar", address_line2=None, city="Lahore", logo_url=None, currency_code="PKR",
)
ARABIC = re.compile("[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def _sale(*, customer: bool = True, urdu_names: bool = False):
    """The sample sale; Urdu is stripped from the DATA so only captions can contain it."""
    sale = mock_sale(BIZ)
    sale.branch_name = "Model Town"
    for line in sale.lines:
        if not urdu_names:
            line.product_name_secondary = None
    sale.customer = CustomerInfoForInvoice(name="Ahmed Khan", phone="0321-7654321") if customer else None
    return sale


def _model(language, design="thermal_classic", channel="print", sale=None, items=None, **extra):
    cfg = default_config(design)
    cfg["style"] = {**cfg["style"], "language": language}
    cfg["items"] = {**cfg["items"], "show_batch": True, "show_expiry": True, "show_sku": True, **(items or {})}
    cfg["meta"] = {**cfg["meta"], "show_branch": True}
    for group, values in extra.items():
        cfg[group] = {**cfg[group], **values}
    view = LayoutView.build("id", "n", design, DESIGN_PAPER[design], cfg)
    return build_receipt_model(sale or _sale(), view, channel, zone=ZONE, qr_url=SAMPLE_LINK)


def _body_text(html: str) -> str:
    html = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html)


ENGLISH_CAPTIONS = [
    "Receipt", "Date", "Cashier", "Customer", "Branch", "Item", "Qty", "Price", "Amount", "SKU", "Batch",
    "Exp", "Subtotal", "Discount", "Total", "Paid", "Change", "Thank you", "NTN", "GST", "Cash",
    "Prices include tax", "Rounding", "Download",
]


# -- one language per caption --------------------------------------------------


@pytest.mark.parametrize("design", DESIGNS)
def test_en_has_no_urdu_caption_anywhere(design):
    model = _model("en", design)
    html, text = render_html(model, pdf_href="/x.pdf"), render_text(model)
    assert not ARABIC.search(_body_text(html)), _body_text(html)
    assert not ARABIC.search(text), text
    assert model["language"] == "en" and model["rtl"] is False and model["dir"] == "ltr"
    assert "lang='en' dir='ltr'" in html
    assert render_pdf(model)[:4] == b"%PDF"


@pytest.mark.parametrize("design", DESIGNS)
def test_ur_has_no_english_caption_anywhere(design):
    model = _model("ur", design)
    html, text = render_html(model, pdf_href="/x.pdf"), render_text(model)
    body = _body_text(html)
    for caption in ENGLISH_CAPTIONS:
        assert caption not in body, (design, caption, body)
        assert caption not in text, (design, caption, text)
    assert ARABIC.search(body) and ARABIC.search(text)
    assert render_pdf(model)[:4] == b"%PDF"


def test_ur_walk_in_and_tax_label_and_ntn_are_urdu():
    model = _model("ur", sale=_sale(customer=False))
    text = render_text(model)
    assert "Walk-in" not in text and "NTN" not in text and "GST" not in text
    assert "واک" in text  # walk-in caption


def test_ur_footer_never_falls_back_to_the_english_text():
    cfg_footer = {"text_en": "Visit again soon", "text_ur": ""}
    model = _model("ur", footer=cfg_footer)
    assert model["footer"]["lines"] == [LABEL_DEFAULTS["thank_you"][1]]
    assert "Visit again" not in render_text(model)


def test_en_footer_uses_english_text_only():
    model = _model("en", footer={"text_en": "", "text_ur": "شکریہ"})
    assert model["footer"]["lines"] == [LABEL_DEFAULTS["thank_you"][0]]


def test_both_prints_two_lines_en_then_ur():
    model = _model("both", footer={"text_en": "See you", "text_ur": "دوبارہ آئیں"})
    assert model["footer"]["lines"] == ["See you", "دوبارہ آئیں"]
    text = render_text(model)
    lines = text.split("\n")
    # every caption is two consecutive lines: English first, then Urdu
    i = next(n for n, line in enumerate(lines) if line.startswith("*Total"))
    assert lines[i].startswith("*Total:") and ARABIC.search(lines[i + 1]) and "960.00" in lines[i + 1]
    j = lines.index("*Receipt*")
    assert ARABIC.search(lines[j + 1])
    assert lines.index("See you") + 1 == lines.index("دوبارہ آئیں")
    html = render_html(model)
    assert "Total<br>" in html and "Receipt<br>" in html
    assert model["rtl"] is False and "dir='ltr'" in html


def test_both_pdf_is_taller_than_en_pdf():
    def height(language):
        pdf = render_pdf(_model(language))
        m = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        return float(m.group(2))

    assert height("both") > height("en")


def test_names_follow_the_language():
    sale = _sale(urdu_names=True)
    en = _model("en", sale=sale)["items"][0]
    ur = _model("ur", sale=sale)["items"][0]
    both = _model("both", sale=sale)["items"][0]
    assert en["name"] == "Panadol Extra 500mg" and en["name_secondary"] is None
    assert ARABIC.search(ur["name"]) and ur["name_secondary"] is None
    assert both["name"] == "Panadol Extra 500mg" and ARABIC.search(both["name_secondary"])


def test_every_caption_has_both_languages():
    for key, (en, ur) in LABEL_DEFAULTS.items():
        assert en.strip() and ur.strip(), key
        assert ARABIC.search(ur), key


def test_label_overrides_apply_per_language():
    cfg = default_config("thermal_classic")
    cfg["labels"]["total"] = {"en": "Grand total", "ur": "مکمل رقم"}
    for language, want in (("en", "Grand total"), ("ur", "مکمل رقم")):
        cfg["style"]["language"] = language
        view = LayoutView.build("i", "n", "thermal_classic", "roll80", cfg)
        model = build_receipt_model(_sale(), view, "print", zone=ZONE)
        label = next(t["label"] for t in model["totals"] if t["key"] == "total")
        assert label.startswith(want) and (language == "ur" or "مکمل" not in label)


# -- right-to-left ------------------------------------------------------------


def test_rtl_flag_and_mirroring_html_text():
    model = _model("ur")
    assert model["rtl"] is True and model["dir"] == "rtl"
    html = render_html(model)
    assert "lang='ur' dir='rtl'" in html and "class='r roll80 rtl" in html
    assert ".rtl{direction:rtl" in html
    text = render_text(model)
    assert all(line.startswith("‏") for line in text.split("\n") if line and not line.startswith("http"))
    assert not any(line.startswith("‏") for line in render_text(_model("en")).split("\n"))


@pytest.mark.parametrize("language", ["ur", "both"])
@pytest.mark.parametrize("design,paper", [
    ("thermal_classic", "roll58"), ("thermal_classic", "roll80"), ("thermal_compact", "roll58"),
    ("a4_tax_invoice", "a4"), ("a4_elegant", "a4"),
])
def test_pdf_renders_urdu_modes_without_missing_glyphs(design, paper, language, caplog):
    """Latin values after Urdu text must keep the Latin font (a stale fallback font printed blanks)."""
    cfg = default_config(design)
    cfg["style"]["language"] = language
    cfg["items"]["show_batch"] = cfg["items"]["show_expiry"] = True
    view = LayoutView.build("i", "n", design, paper, cfg)
    with caplog.at_level("WARNING"):
        pdf = render_pdf(build_receipt_model(_sale(urdu_names=True), view, "print", zone=ZONE, qr_url=SAMPLE_LINK))
    assert pdf[:4] == b"%PDF"
    assert "missing the following glyphs" not in caplog.text, caplog.text


# -- config version 2 -----------------------------------------------------------


def test_default_config_is_v2_with_language_and_no_name_language():
    for design in DESIGNS:
        cfg = default_config(design)
        assert cfg["v"] == 2 and cfg["style"]["language"] == "en"
        assert "name_language" not in cfg["items"]


def test_old_v1_config_is_upgraded_and_name_language_dropped():
    old = {"v": 1, "items": {"show_sku": True, "name_language": "both"}, "style": {"font_scale": 1.1}}
    cfg = normalise_config(old)
    assert cfg["v"] == 2 and cfg["style"]["language"] == "en" and cfg["style"]["font_scale"] == 1.1
    assert cfg["items"]["show_sku"] is True and "name_language" not in cfg["items"]
    assert old["items"]["name_language"] == "both"  # the input is not modified


def test_v1_with_edited_urdu_captions_or_footer_becomes_both():
    edited = normalise_config({"v": 1, "labels": {"total": {"ur": "کل رقم"}}})
    assert edited["style"]["language"] == "both"
    footer = normalise_config({"v": 1, "footer": {"text_ur": "شکریہ"}})
    assert footer["style"]["language"] == "both"
    # a full v1 config whose Urdu captions are just the built-in text stays English
    full_default = {"v": 1, "labels": {k: {"en": en, "ur": ur} for k, (en, ur) in LABEL_DEFAULTS.items()}}
    assert normalise_config(full_default)["style"]["language"] == "en"


def test_explicit_language_wins_and_bad_values_are_rejected():
    assert normalise_config({"v": 1, "style": {"language": "ur"}})["style"]["language"] == "ur"
    assert normalise_config({"style": {"language": "both"}})["style"]["language"] == "both"
    with pytest.raises(Exception):
        normalise_config({"v": 2, "style": {"language": "fr"}})
    with pytest.raises(ValueError):
        upgrade_config({"v": 3})


def test_upgrade_is_idempotent_and_v2_name_language_is_dropped_on_save():
    once = normalise_config({"v": 1, "items": {"name_language": "secondary"}})
    assert normalise_config(once) == once
    assert "name_language" not in normalise_config({"v": 2, "items": {"name_language": "both"}})["items"]


def test_old_config_still_renders():
    view = LayoutView.build("i", "n", "thermal_classic", "roll80", {"v": 1, "items": {"name_language": "both"}})
    assert view.config["v"] == 2
    assert render_text(build_receipt_model(_sale(), view, "print", zone=ZONE))


# -- legacy receipt options -> layout config -------------------------------------


def test_read_legacy_only_reports_changes():
    assert read_legacy({}, None).empty
    defaults = {
        "receipt.footer_text": {"value": "Thank you for shopping with us!"},
        "receipt.paper_size": {"value": "80mm"},
        "receipt.show_tax": {"value": True},
        "receipt.show_logo": {"value": True},
    }
    assert read_legacy(defaults, None).empty
    changed = read_legacy(
        {"receipt.footer_text": {"value": "Come again"}, "receipt.paper_size": {"value": "58mm"},
         "receipt.show_tax": {"value": False}, "receipt.show_logo": {"value": "false"}},
        None,
    )
    assert (changed.footer_en, changed.paper, changed.show_tax, changed.show_logo) == ("Come again", "roll58", False, False)


def test_read_legacy_urdu_footer_and_business_config_fallback():
    ur = read_legacy({"receipt.footer_text": {"value": "شکریہ"}}, None)
    assert ur.footer_ur == "شکریہ" and ur.footer_en == ""
    cfg = read_legacy({}, {"receipt_footer": "From config", "receipt": {"paper_size": "58", "show_logo": False}})
    assert cfg.footer_en == "From config" and cfg.paper == "roll58" and cfg.show_logo is False
    # app_settings win over the business config
    both = read_legacy({"receipt.footer_text": {"value": "Setting"}}, {"receipt_footer": "Config"})
    assert both.footer_en == "Setting"


def test_apply_legacy_copies_changed_values_only():
    raw = default_config("thermal_classic")
    raw["v"] = 1
    raw["items"]["name_language"] = "primary"
    del raw["style"]["language"]
    legacy = LegacyOptions(footer_en="Come again", paper="roll58", show_tax=False, show_logo=False)
    cfg, paper = apply_legacy(raw, "thermal_classic", "roll80", legacy)
    assert cfg["v"] == 2 and cfg["style"]["language"] == "en"
    assert cfg["footer"]["text_en"] == "Come again" and cfg["footer"]["text_ur"] == ""
    assert cfg["totals"]["show_tax_breakdown"] is False and cfg["header"]["show_logo"] is False
    assert paper == "roll58"
    # nothing changed -> nothing copied
    untouched, paper2 = apply_legacy(raw, "thermal_classic", "roll80", LegacyOptions())
    assert untouched["footer"]["text_en"] == "" and untouched["totals"]["show_tax_breakdown"] is True
    assert untouched["header"]["show_logo"] is True and paper2 == "roll80"
    # A4 paper never changes a thermal paper
    assert apply_legacy(raw, "a4_tax_invoice", "a4", LegacyOptions(paper="roll58"))[1] == "a4"


def test_apply_legacy_urdu_edits_map_to_both():
    raw = {"v": 1, "labels": {"total": {"ur": "کل رقم"}}}
    assert apply_legacy(raw, "thermal_classic", "roll80", LegacyOptions())[0]["style"]["language"] == "both"
    assert apply_legacy({"v": 1}, "thermal_classic", "roll80",
                        LegacyOptions(footer_ur="شکریہ"))[0]["style"]["language"] == "both"
    assert apply_legacy({"v": 1}, "thermal_classic", "roll80", LegacyOptions(footer_en="x"))[0]["style"]["language"] == "en"
