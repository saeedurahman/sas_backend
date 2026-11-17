"""Receipt language: layout config v2, invoice/public page language, legacy options migration."""

from __future__ import annotations

import json
import re

import pytest

from app.services.receipt_legacy import run_migration
from tests.helpers.assertions import assert_status
from tests.helpers.db import _engine, db_execute, db_rows, db_scalar
from tests.helpers.phase2 import post_sale
from tests.helpers.sync import by_entity, snapshot_all
from tests.integration.tax.helpers import put_setting

pytestmark = pytest.mark.integration

ARABIC = re.compile("[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
THANK_YOU_EN = "Thank you for shopping with us!"


def _layouts(c, t) -> list[dict]:
    r = c.get(t.url("/receipt-layouts"), headers=t.owner_headers)
    assert_status(r, 200, label="list layouts")
    return r.json()


def _by_design(c, t, design: str) -> dict:
    return next(x for x in _layouts(c, t) if x["design"] == design)


def _set_language(c, t, layout: dict, language: str) -> None:
    cfg = {**layout["config"], "style": {**layout["config"]["style"], "language": language}}
    r = c.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers, json={"config": cfg})
    assert_status(r, 200, label="set language")
    assert r.json()["config"]["style"]["language"] == language


def _sale(c, t) -> dict:
    r = post_sale(c, t, qty="2", unit_price="150.00")
    assert_status(r, 201, label="sale")
    return r.json()


def _page_text(html: str) -> str:
    html = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# -- config v2 over the API ---------------------------------------------------


def test_new_layouts_are_v2_english_without_name_language(http_client, pos_tenant):
    for layout in _layouts(http_client, pos_tenant):
        assert layout["config"]["v"] == 2 and layout["config"]["style"]["language"] == "en"
        assert "name_language" not in layout["config"]["items"]


def test_save_accepts_old_config_and_drops_name_language(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_classic")
    r = http_client.put(
        t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
        json={"config": {"v": 1, "items": {"show_sku": True, "name_language": "both"}}},
    )
    assert_status(r, 200, label="save old config")
    cfg = r.json()["config"]
    assert cfg["v"] == 2 and cfg["style"]["language"] == "en" and cfg["items"]["show_sku"] is True
    assert "name_language" not in cfg["items"]
    stored = db_scalar("SELECT config FROM receipt_layouts WHERE id = :i", {"i": layout["id"]})
    assert stored["v"] == 2 and "name_language" not in stored["items"]


def test_bad_language_and_future_version_are_rejected(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_classic")
    for cfg in ({"v": 2, "style": {"language": "fr"}}, {"v": 3}):
        r = http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
                            json={"config": cfg})
        assert_status(r, 422, label=f"reject {cfg}")


def test_stored_v1_config_is_upgraded_on_read_and_in_the_feed(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_compact")
    old = {"v": 1, "items": {"name_language": "secondary"},
           "labels": {"total": {"en": "Total", "ur": "کل رقم"}}}
    db_execute("UPDATE receipt_layouts SET config = CAST(:c AS jsonb) WHERE id = :i",
               {"c": json.dumps(old), "i": layout["id"]})
    got = http_client.get(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers)
    assert_status(got, 200, label="get old layout")
    cfg = got.json()["config"]
    assert cfg["v"] == 2 and "name_language" not in cfg["items"]
    assert cfg["style"]["language"] == "both"  # an edited Urdu caption
    changes, _, _ = snapshot_all(http_client, t)
    wire = next(c for c in by_entity(changes, "receipt_layout") if c["id"] == layout["id"])
    assert wire["data"]["config"]["v"] == 2 and "name_language" not in wire["data"]["config"]["items"]


# -- design change on update --------------------------------------------------


def test_design_can_change_on_update_and_the_config_is_kept(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_classic")
    cfg = {**layout["config"], "style": {**layout["config"]["style"], "language": "both"},
           "footer": {**layout["config"]["footer"], "text_en": "Kept footer"}}
    # thermal -> thermal: the paper in use stays unless a new one is named
    r = http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
                        json={"design": "thermal_compact", "config": cfg})
    assert_status(r, 200, label="classic -> compact")
    body = r.json()
    assert (body["design"], body["paper"]) == ("thermal_compact", "roll80")
    assert body["config"]["style"]["language"] == "both" and body["config"]["footer"]["text_en"] == "Kept footer"
    assert db_scalar("SELECT design FROM receipt_layouts WHERE id = :i", {"i": layout["id"]}) == "thermal_compact"

    # thermal -> A4 without a paper: the new design's own paper; config untouched (no config sent)
    r = http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
                        json={"design": "a4_tax_invoice"})
    assert_status(r, 200, label="compact -> a4")
    body = r.json()
    assert (body["design"], body["paper"]) == ("a4_tax_invoice", "a4")
    assert body["config"]["style"]["language"] == "both" and body["config"]["footer"]["text_en"] == "Kept footer"

    # and back, naming a paper explicitly
    r = http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
                        json={"design": "thermal_classic", "paper": "roll58"})
    assert_status(r, 200, label="a4 -> classic roll58")
    assert (r.json()["design"], r.json()["paper"]) == ("thermal_classic", "roll58")

    # the changed layout renders with its new design (preview + public receipt model)
    sale = _sale(http_client, t)
    model = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                            params={"channel": "print"}).json()
    assert model["layout"]["design"] == "thermal_classic" and model["paper"]["code"] == "roll58"


def test_design_change_rejects_bad_combinations_and_keeps_the_layout(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "a4_elegant")
    url = t.url(f"/receipt-layouts/{layout['id']}")
    for body in (
        {"design": "nope"},
        {"design": "thermal_classic", "paper": "a4"},  # thermal design on A4
        {"design": "a4_tax_invoice", "paper": "roll58"},  # A4 design on a roll
        {"design": "thermal_classic", "config": {"v": 2, "style": {"language": "fr"}}},  # bad config
    ):
        assert_status(http_client.put(url, headers=t.owner_headers, json=body), 422, label=str(body))
    row = db_rows("SELECT design, paper FROM receipt_layouts WHERE id = :i", {"i": layout["id"]})[0]
    assert (row["design"], row["paper"]) == ("a4_elegant", "a4")  # nothing half-applied
    # same design is a no-op change that still validates
    assert_status(http_client.put(url, headers=t.owner_headers, json={"design": "a4_elegant"}), 200, label="same")
    # the audit trail records the design change
    http_client.put(url, headers=t.owner_headers, json={"design": "a4_tax_invoice"})
    audit = db_scalar(
        "SELECT count(*) FROM audit_logs WHERE record_id = :i AND table_name = 'receipt_layouts' "
        "AND action = 'update' AND new_values->>'design' = 'a4_tax_invoice'", {"i": layout["id"]})
    assert int(audit) == 1


def test_design_change_needs_settings_manage(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_classic")
    r = http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.cashier_headers,
                        json={"design": "thermal_compact"})
    assert_status(r, 403, label="cashier")


# -- invoice endpoints + public page follow the layout language ----------------


def test_invoice_endpoints_and_public_page_follow_the_language(http_client, pos_tenant):
    t = pos_tenant
    classic = _by_design(http_client, t, "thermal_classic")  # default for print, whatsapp and link
    sale = _sale(http_client, t)
    base = t.url(f"/invoice/{sale['id']}/receipt")

    # en
    en_text = http_client.get(base, headers=t.owner_headers, params={"format": "text"}).text
    assert THANK_YOU_EN in en_text and not ARABIC.search(en_text)
    link = http_client.post(t.url(f"/sales/{sale['id']}/receipt-link"), headers=t.owner_headers).json()
    page = http_client.get(link["url"])
    assert "lang='en' dir='ltr'" in page.text and not ARABIC.search(_page_text(page.text))

    # ur
    _set_language(http_client, t, classic, "ur")
    model = http_client.get(base, headers=t.owner_headers).json()
    assert model["language"] == "ur" and model["rtl"] is True and model["dir"] == "rtl"
    assert model["footer"]["lines"] == ["خریداری کا شکریہ!"]
    ur_text = http_client.get(base, headers=t.owner_headers, params={"format": "text"}).text
    assert "Total" not in ur_text and "Thank you" not in ur_text and ARABIC.search(ur_text)
    ur_html = http_client.get(base, headers=t.owner_headers, params={"format": "html"}).text
    assert "lang='ur' dir='rtl'" in ur_html
    assert http_client.get(base, headers=t.owner_headers, params={"format": "pdf"}).content[:4] == b"%PDF"
    thermal = http_client.get(t.url(f"/invoice/{sale['id']}/thermal"), headers=t.owner_headers).json()
    assert thermal["receipt"]["language"] == "ur"
    assert thermal["footer"] == "خریداری کا شکریہ!"
    assert thermal["paper_size"] == "80mm"

    page = http_client.get(link["url"])
    assert page.status_code == 200
    assert "lang='ur' dir='rtl'" in page.text and "class='r roll80 rtl" in page.text
    body = _page_text(page.text)
    assert "Total" not in body and "Thank you" not in body and "Download PDF" not in body and ARABIC.search(body)
    assert http_client.get(link["url"], params={"format": "pdf"}).content[:4] == b"%PDF"

    # both: two lines
    _set_language(http_client, t, classic, "both")
    page = http_client.get(link["url"])
    assert "lang='en' dir='ltr'" in page.text
    assert "Total<br>" in page.text and THANK_YOU_EN in page.text
    both = http_client.get(base, headers=t.owner_headers).json()
    assert both["language"] == "both" and len(both["footer"]["lines"]) == 2


def test_each_channel_follows_its_own_layout_language(http_client, pos_tenant):
    t = pos_tenant
    compact = _by_design(http_client, t, "thermal_compact")
    _set_language(http_client, t, compact, "ur")
    r = http_client.put(
        t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
        json={"assignments": [{"channel": "whatsapp", "branch_id": None, "layout_id": compact["id"]}]},
    )
    assert_status(r, 200, label="assign whatsapp")
    sale = _sale(http_client, t)
    base = t.url(f"/invoice/{sale['id']}/receipt")
    wa = http_client.get(base, headers=t.owner_headers, params={"channel": "whatsapp"}).json()
    pr = http_client.get(base, headers=t.owner_headers, params={"channel": "print"}).json()
    assert wa["language"] == "ur" and pr["language"] == "en"


# -- settings subset -----------------------------------------------------------


def test_legacy_receipt_keys_are_readable_but_not_in_the_sync_settings(http_client, pos_tenant):
    t = pos_tenant
    assert put_setting(http_client, t, "receipt.footer_text", "Legacy footer").status_code == 200
    r = http_client.get(t.url("/settings"), headers=t.owner_headers)
    assert_status(r, 200, label="settings")
    row = next(x for x in r.json() if x["setting_key"] == "receipt.footer_text")
    assert row["setting_value"] == {"value": "Legacy footer"}
    changes, _, _ = snapshot_all(http_client, t)
    data = by_entity(changes, "settings")[0]["data"]
    assert "receipt_footer" not in data and "receipt.footer_text" not in data["app_settings"]
    assert "receipt.number_prefix" in data["app_settings"]  # numbering keys stay


def test_legacy_footer_setting_does_not_render(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "receipt.footer_text", "LEGACY-FOOTER-XYZ")
    sale = _sale(http_client, t)
    out = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                          params={"format": "text"}).text
    assert "LEGACY-FOOTER-XYZ" not in out and THANK_YOU_EN in out


# -- migration copies legacy values once ---------------------------------------


def _v1_reset(business_id: str) -> None:
    """Put the business's seeded layouts back to the pre-028 shape (v1, bilingual default captions)."""
    for row in db_rows("SELECT id, design, config FROM receipt_layouts WHERE business_id = :b", {"b": business_id}):
        cfg = row["config"]
        cfg["v"] = 1
        cfg["style"].pop("language", None)
        cfg["items"]["name_language"] = "primary"
        db_execute("UPDATE receipt_layouts SET config = CAST(:c AS jsonb), paper = :p WHERE id = :i",
                   {"c": json.dumps(cfg), "i": row["id"],
                    "p": "roll80" if row["design"] == "thermal_classic" else
                    "roll58" if row["design"] == "thermal_compact" else "a4"})


def _legacy_setting(business_id: str, key: str, value) -> None:
    db_execute(
        "INSERT INTO app_settings (id, business_id, branch_id, setting_key, setting_value, created_at, updated_at) "
        "VALUES (gen_random_uuid(), :b, NULL, :k, CAST(:v AS jsonb), now(), now()) "
        "ON CONFLICT DO NOTHING",
        {"b": business_id, "k": key, "v": json.dumps({"value": value})},
    )


def _layout_rows(business_id: str) -> dict[str, dict]:
    return {r["design"]: r for r in db_rows(
        "SELECT design, paper, config, updated_at FROM receipt_layouts WHERE business_id = :b AND deleted_at IS NULL",
        {"b": business_id})}


def _run() -> dict:
    with _engine().begin() as conn:
        return run_migration(conn)


def test_migration_copies_changed_legacy_values_once(http_client, pos_tenant):
    t = pos_tenant
    b = t.business_id
    _v1_reset(b)
    _legacy_setting(b, "receipt.footer_text", "Come again soon")
    _legacy_setting(b, "receipt.paper_size", "58mm")
    _legacy_setting(b, "receipt.show_tax", False)
    _legacy_setting(b, "receipt.show_logo", False)

    first = _run()
    assert first["upgraded"] >= 4 and first["copied"] >= 4
    rows = _layout_rows(b)
    for design, row in rows.items():
        cfg = row["config"]
        assert cfg["v"] == 2 and cfg["style"]["language"] == "en" and "name_language" not in cfg["items"]
        assert cfg["footer"]["text_en"] == "Come again soon" and cfg["footer"]["text_ur"] == ""
        assert cfg["totals"]["show_tax_breakdown"] is False and cfg["header"]["show_logo"] is False
    assert rows["thermal_classic"]["paper"] == "roll58" and rows["thermal_compact"]["paper"] == "roll58"
    assert rows["a4_tax_invoice"]["paper"] == "a4" and rows["a4_elegant"]["paper"] == "a4"

    # once: a second run (and a later settings change) does not touch migrated layouts
    db_execute("UPDATE app_settings SET setting_value = CAST(:v AS jsonb) WHERE business_id = :b "
               "AND setting_key = 'receipt.footer_text'", {"v": json.dumps({"value": "Changed later"}), "b": b})
    again = _run()
    assert again["upgraded"] == 0
    assert _layout_rows(b)["thermal_classic"]["config"]["footer"]["text_en"] == "Come again soon"
    assert _layout_rows(b)["thermal_classic"]["updated_at"] == rows["thermal_classic"]["updated_at"]

    # the legacy rows are left in place (still readable) ...
    assert int(db_scalar("SELECT count(*) FROM app_settings WHERE business_id = :b AND setting_key LIKE 'receipt.%'",
                         {"b": b})) >= 4
    # ... and the migrated layout shows up through the API and renders its footer
    layout = _by_design(http_client, t, "thermal_classic")
    assert layout["config"]["footer"]["text_en"] == "Come again soon" and layout["paper"] == "roll58"
    sale = _sale(http_client, t)
    out = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                          params={"format": "text"}).text
    assert "Come again soon" in out and "Changed later" not in out


def test_migration_copies_nothing_when_options_are_the_defaults(http_client, pos_tenant):
    t = pos_tenant
    b = t.business_id
    _v1_reset(b)
    _legacy_setting(b, "receipt.footer_text", THANK_YOU_EN)
    _legacy_setting(b, "receipt.paper_size", "80mm")
    _legacy_setting(b, "receipt.show_tax", True)
    _legacy_setting(b, "receipt.show_logo", True)
    _run()
    for row in _layout_rows(b).values():
        cfg = row["config"]
        assert cfg["footer"]["text_en"] == "" and cfg["footer"]["text_ur"] == ""
        assert cfg["totals"]["show_tax_breakdown"] is True and cfg["header"]["show_logo"] is True
    assert _layout_rows(b)["thermal_classic"]["paper"] == "roll80"
    assert _layout_rows(b)["thermal_compact"]["paper"] == "roll58"


def test_migration_maps_edited_urdu_to_both_and_urdu_footer_to_text_ur(http_client, pos_tenant):
    t = pos_tenant
    b = t.business_id
    _v1_reset(b)
    classic = _by_design(http_client, t, "thermal_classic")
    cfg = db_scalar("SELECT config FROM receipt_layouts WHERE id = :i", {"i": classic["id"]})
    cfg["labels"]["total"]["ur"] = "کل رقم"
    db_execute("UPDATE receipt_layouts SET config = CAST(:c AS jsonb) WHERE id = :i",
               {"c": json.dumps(cfg), "i": classic["id"]})
    _legacy_setting(b, "receipt.footer_text", "شکریہ")
    _run()
    rows = _layout_rows(b)
    assert rows["thermal_classic"]["config"]["style"]["language"] == "both"
    # the Urdu footer text is a Urdu edit for every seeded layout
    assert all(r["config"]["style"]["language"] == "both" for r in rows.values())
    assert all(r["config"]["footer"]["text_ur"] == "شکریہ" and
               r["config"]["footer"]["text_en"] == "" for r in rows.values())


def test_migration_legacy_a4_makes_the_a4_invoice_the_print_default(http_client, pos_tenant):
    t = pos_tenant
    b = t.business_id
    _v1_reset(b)
    _legacy_setting(b, "receipt.paper_size", "A4")
    _run()
    a4 = _by_design(http_client, t, "a4_tax_invoice")
    rows = http_client.get(t.url("/receipt-layouts/assignments"), headers=t.owner_headers).json()
    by_channel = {a["channel"]: a["layout_id"] for a in rows if a["branch_id"] is None}
    assert by_channel["print"] == a4["id"]
    assert by_channel["whatsapp"] != a4["id"] and by_channel["link"] != a4["id"]


def test_migration_reads_the_business_config_fallback(http_client, pos_tenant):
    t = pos_tenant
    b = t.business_id
    _v1_reset(b)
    db_execute(
        "UPDATE business_configs SET config_json = CAST(:c AS jsonb) WHERE business_id = :b",
        {"c": json.dumps({"receipt_footer": "From business config", "receipt": {"show_logo": False}}), "b": b},
    )
    _run()
    cfg = _layout_rows(b)["thermal_classic"]["config"]
    assert cfg["footer"]["text_en"] == "From business config" and cfg["header"]["show_logo"] is False
