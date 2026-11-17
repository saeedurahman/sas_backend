"""Receipt render model + renderers: pure functions, no server."""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.services.receipt_layout_config import (
    DESIGN_PAPER,
    DESIGNS,
    default_config,
    normalise_config,
)
from app.services.receipt_layout_service import SAMPLE_LINK, mock_sale
from app.services.receipt_render import (
    LayoutView,
    build_receipt_model,
    clean_ntn,
    qty_text,
    render_html,
    render_pdf,
    render_text,
)

ZONE = ZoneInfo("Asia/Karachi")
BIZ = SimpleNamespace(
    name="Al Shifa Pharmacy", legal_name=None, phone="0300-1", tax_id="1234567-8",
    address_line1="Main Bazaar", address_line2=None, city="Lahore", logo_url=None, currency_code="PKR",
)


def _view(design="thermal_classic", **patch):
    cfg = default_config(design)
    for group, values in patch.items():
        cfg[group] = {**cfg[group], **values}
    return LayoutView.build("id", "n", design, DESIGN_PAPER[design], cfg)


def _model(design="thermal_classic", channel="print", qr=SAMPLE_LINK, sale=None, **patch):
    return build_receipt_model(sale or mock_sale(BIZ), _view(design, **patch), channel, zone=ZONE, qr_url=qr)


def _text(model) -> str:
    return render_text(model)


# -- config -------------------------------------------------------------------


def test_defaults_filled_and_unknown_keys_rejected():
    cfg = normalise_config({"v": 1})
    assert cfg["items"]["show_name"] is True and cfg["style"]["font_scale"] == 1.0
    with pytest.raises(ValidationError):
        normalise_config({"v": 1, "items": {"show_colour": True}})
    with pytest.raises(ValidationError):
        normalise_config({"v": 1, "bogus": {}})
    with pytest.raises(ValidationError):
        normalise_config({"v": 1, "style": {"font_scale": 3}})
    with pytest.raises(ValidationError):
        normalise_config({"v": 1, "header": {"extra_lines": ["a", "b", "c", "d"]}})


def test_pharmacy_defaults_turn_batch_and_expiry_on():
    assert default_config("thermal_classic")["items"]["show_batch"] is False
    ph = default_config("thermal_classic", pharmacy=True)["items"]
    assert ph["show_batch"] is True and ph["show_expiry"] is True


# -- rules --------------------------------------------------------------------


def test_no_internal_sync_or_status_text():
    for design in DESIGNS:
        model = _model(design)
        out = (_text(model) + render_html(model)).lower()
        for bad in ("pending", "sync", "unsynced"):
            assert bad not in out, (design, bad)


def test_single_number_follows_source():
    reg = _model(meta={"number_source": "register"})
    off = _model(meta={"number_source": "official"})
    reg_nums = [m for m in reg["meta"] if m["key"] == "number"]
    off_nums = [m for m in off["meta"] if m["key"] == "number"]
    assert len(reg_nums) == len(off_nums) == 1
    assert reg_nums[0]["value"] == "C1-0087"
    assert off_nums[0]["value"] == "INV-0001042"
    sale = mock_sale(BIZ)
    sale.client_receipt_no = None  # register falls back to the official number
    fallback = _model(sale=sale, meta={"number_source": "register"})["meta"]
    assert [m["value"] for m in fallback if m["key"] == "number"] == ["INV-0001042"]


def test_variation_only_when_real():
    sale = mock_sale(BIZ)
    sale.lines[1].variation_name = None
    names = [i["name"] for i in _model(sale=sale, items={"show_variation": True})["items"]]
    assert names == ["Panadol Extra 500mg", "Basmati Rice"]
    sale = mock_sale(BIZ)
    shown = [i["name"] for i in _model(sale=sale, items={"show_variation": True})["items"]]
    assert shown[1] != "Basmati Rice"  # the real "5 kg" variation is shown
    hidden = [i["name"] for i in _model(sale=sale, items={"show_variation": False})["items"]]
    assert hidden[1] == "Basmati Rice"


def test_quantity_format():
    assert qty_text(Decimal("2.0000"), False) == "2"
    assert qty_text(Decimal("1.5000"), True) == "1.5"
    assert qty_text(Decimal("3.0000"), True) == "3"
    assert [i["qty"] for i in _model()["items"]] == ["2", "1.5"]


def test_ntn_label_and_tax_rate_never_printed_as_ntn():
    assert clean_ntn("GST 17%") is None
    assert clean_ntn("17%") is None
    assert clean_ntn("VAT 5") is None
    assert clean_ntn("1234567-8") == "1234567-8"
    out = _text(_model(header={"show_ntn": True, "ntn_label": "NTN"}))
    assert "NTN: 1234567-8" in out
    sale = mock_sale(BIZ)
    sale.business.tax_id = "GST 17%"
    model = _model(sale=sale, header={"show_ntn": True})
    assert model["header"]["ntn"] is None
    assert "NTN: GST" not in _text(model)


def test_qr_only_when_synced_and_print():
    on = _model(channel="print", footer={"show_qr": True})
    assert on["qr"] and on["qr"]["content"] == SAMPLE_LINK
    sale = mock_sale(BIZ)
    sale.synced = False
    assert _model(sale=sale, qr=None, footer={"show_qr": True})["qr"] is None
    assert _model(sale=sale, qr=SAMPLE_LINK, footer={"show_qr": True})["qr"] is None
    assert _model(channel="link", footer={"show_qr": True})["qr"] is None


def test_link_channel_hides_customer_and_cashier():
    m = _model(channel="link", meta={"show_customer": True, "show_cashier": True, "show_customer_phone": True})
    out = _text(m)
    assert "احمد" not in out and "0321" not in out and "Ali" not in out


def test_batch_expiry_lines_follow_layout():
    on = _text(_model(items={"show_batch": True, "show_expiry": True}))
    assert "B2407" in on
    off = _text(_model(items={"show_batch": False, "show_expiry": False}))
    assert "B2407" not in off
    assert re.search(r"\b\d{2}/\d{4}\b", on)


def test_language_drives_names_and_captions():
    both = _text(_model(style={"language": "both"}))
    assert "پینا" in both
    ur_name = _model(style={"language": "ur"})["items"][0]["name"]
    assert ur_name == "پینا ڈول ایکسٹرا"
    labels = " ".join(t["label"] for t in _model(style={"language": "both"})["totals"])
    assert "Total" in labels and re.search(r"[؀-ۿ]", labels)


def test_totals_come_from_the_snapshot():
    totals = {t["key"]: t["value"] for t in _model(totals={"show_rounding": True})["totals"]}
    assert totals["total"] == "960.00"
    assert totals["rounding"].lstrip("+") == "0.60"


@pytest.mark.parametrize("design", DESIGNS)
def test_every_design_renders_html_text_pdf(design):
    for channel in ("print", "whatsapp", "link"):
        model = _model(design, channel=channel)
        assert "<html" in render_html(model).lower()
        assert _text(model)
        assert render_pdf(model)[:4] == b"%PDF"


def _page_size_mm(pdf: bytes) -> tuple[float, float]:
    m = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
    assert m, "no MediaBox"
    pt_to_mm = 25.4 / 72
    return float(m.group(1)) * pt_to_mm, float(m.group(2)) * pt_to_mm


def test_pdf_widths_are_exact():
    for paper, width in (("roll58", 58), ("roll80", 80)):
        view = LayoutView.build("i", "n", "thermal_classic", paper, default_config("thermal_classic"))
        model = build_receipt_model(mock_sale(BIZ), view, "print", zone=ZONE, qr_url=SAMPLE_LINK)
        w, h = _page_size_mm(render_pdf(model))
        assert abs(w - width) < 0.5, (paper, w)
        assert h > 40
    view = LayoutView.build("i", "n", "a4_tax_invoice", "a4", default_config("a4_tax_invoice"))
    model = build_receipt_model(mock_sale(BIZ), view, "print", zone=ZONE, qr_url=None)
    w, h = _page_size_mm(render_pdf(model))
    assert abs(w - 210) < 1 and abs(h - 297) < 1
