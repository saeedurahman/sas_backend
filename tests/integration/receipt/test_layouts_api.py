"""Receipt layouts: seeding, CRUD, validation, permissions, audit, assignments, preview, feed."""

from __future__ import annotations

import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_rows, db_scalar
from tests.helpers.phase2 import post_sale
from tests.helpers.sync import by_entity, pull_all, snapshot_all

pytestmark = pytest.mark.integration

DESIGNS = {"thermal_classic", "thermal_compact", "a4_tax_invoice", "a4_elegant"}


def _layouts(c, t) -> list[dict]:
    r = c.get(t.url("/receipt-layouts"), headers=t.owner_headers)
    assert_status(r, 200, label="list layouts")
    return r.json()


def _by_design(c, t, design: str) -> dict:
    return next(x for x in _layouts(c, t) if x["design"] == design)


def _assignments(c, t) -> list[dict]:
    r = c.get(t.url("/receipt-layouts/assignments"), headers=t.owner_headers)
    assert_status(r, 200, label="list assignments")
    return r.json()


def _sale(c, t) -> dict:
    r = post_sale(c, t, qty="2", unit_price="150.00")
    assert_status(r, 201, label="sale")
    return r.json()


# -- seeding ------------------------------------------------------------------


def test_new_business_gets_four_layouts_and_thermal_classic_defaults(http_client, pos_tenant):
    t = pos_tenant
    layouts = _layouts(http_client, t)
    assert {x["design"] for x in layouts} == DESIGNS
    classic = next(x for x in layouts if x["design"] == "thermal_classic")
    assert classic["config"]["v"] == 1
    rows = _assignments(http_client, t)
    assert {(a["channel"], a["branch_id"], a["layout_id"]) for a in rows} == {
        (ch, None, classic["id"]) for ch in ("print", "whatsapp", "link")
    }


def test_every_existing_business_was_seeded_by_the_migration():
    missing = db_scalar(
        "SELECT count(*) FROM businesses b WHERE b.deleted_at IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM receipt_layouts l WHERE l.business_id = b.id AND l.deleted_at IS NULL)"
    )
    assert int(missing) == 0


def test_designs_endpoint(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.get(t.url("/receipt-layouts/designs"), headers=t.owner_headers)
    assert_status(r, 200, label="designs")
    body = r.json()
    assert {d["design"] for d in body["designs"]} == DESIGNS
    assert body["channels"] == ["print", "whatsapp", "link"]


# -- CRUD / validation / permissions / audit ----------------------------------


def test_create_get_update_delete(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.post(
        t.url("/receipt-layouts"), headers=t.owner_headers,
        json={"name": "My roll", "design": "thermal_compact", "paper": "roll58",
              "config": {"v": 1, "footer": {"text_en": "Thanks!"}}},
    )
    assert_status(r, 201, label="create")
    layout = r.json()
    assert layout["paper"] == "roll58" and layout["config"]["footer"]["text_en"] == "Thanks!"
    assert layout["config"]["items"]["show_name"] is True  # defaults filled in

    got = http_client.get(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers)
    assert_status(got, 200, label="get")

    upd = http_client.put(
        t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
        json={"name": "Renamed", "config": {"v": 1, "style": {"font_scale": 1.2}}},
    )
    assert_status(upd, 200, label="update")
    assert upd.json()["name"] == "Renamed" and upd.json()["config"]["style"]["font_scale"] == 1.2

    dele = http_client.delete(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers)
    assert_status(dele, 204, label="delete")
    assert all(x["id"] != layout["id"] for x in _layouts(http_client, t))
    assert http_client.get(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "design": "nope"},
        {"name": "x", "design": "a4_elegant", "paper": "roll58"},
        {"name": "x", "design": "thermal_classic", "paper": "a4"},
        {"name": "x", "design": "thermal_classic", "config": {"v": 1, "items": {"show_colour": True}}},
        {"name": "x", "design": "thermal_classic", "config": {"v": 1, "unknown": {}}},
        {"name": "x", "design": "thermal_classic", "config": {"v": 1, "style": {"font_scale": 5}}},
        {"name": "x", "design": "thermal_classic",
         "config": {"v": 1, "header": {"extra_lines": ["a", "b", "c", "d"]}}},
        {"name": "x", "design": "thermal_classic", "config": {"v": 1, "labels": {"made_up": {"en": "x"}}}},
        {"name": "", "design": "thermal_classic"},
    ],
)
def test_validation_rejects_bad_input(http_client, pos_tenant, body):
    t = pos_tenant
    r = http_client.post(t.url("/receipt-layouts"), headers=t.owner_headers, json=body)
    assert r.status_code in (400, 422), r.text


def test_requires_settings_manage(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_classic")
    h = t.cashier_headers
    assert http_client.get(t.url("/receipt-layouts"), headers=h).status_code == 403
    assert http_client.post(t.url("/receipt-layouts"), headers=h,
                            json={"name": "x", "design": "thermal_classic"}).status_code == 403
    assert http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=h,
                           json={"name": "y"}).status_code == 403
    assert http_client.put(t.url("/receipt-layouts/assignments"), headers=h,
                           json={"assignments": [{"channel": "print", "layout_id": layout["id"]}]}
                           ).status_code == 403
    assert http_client.post(t.url("/receipt-layouts/preview"), headers=h,
                            json={"design": "thermal_classic"}).status_code == 403


def test_layout_changes_are_audited_with_before_and_after(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_compact")
    r = http_client.put(
        t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers, json={"name": "Audit me"}
    )
    assert_status(r, 200, label="update")
    rows = db_rows(
        "SELECT old_values, new_values FROM audit_logs WHERE business_id = :b "
        "AND table_name = 'receipt_layouts' AND record_id = :r ORDER BY created_at DESC",
        {"b": t.business_id, "r": layout["id"]},
    )
    assert rows, "no audit row"
    assert "Audit me" in str(rows[0]["new_values"]) and layout["name"] in str(rows[0]["old_values"])


def test_duplicate(http_client, pos_tenant):
    t = pos_tenant
    src = _by_design(http_client, t, "a4_tax_invoice")
    r = http_client.post(
        t.url(f"/receipt-layouts/{src['id']}/duplicate"), headers=t.owner_headers, json={"name": "Copy A4"}
    )
    assert_status(r, 201, label="duplicate")
    copy = r.json()
    assert copy["id"] != src["id"] and copy["name"] == "Copy A4"
    assert (copy["design"], copy["paper"], copy["config"]) == (src["design"], src["paper"], src["config"])


# -- assignments --------------------------------------------------------------


def test_assignment_in_use_cannot_be_deleted_or_deactivated(http_client, pos_tenant):
    t = pos_tenant
    classic = _by_design(http_client, t, "thermal_classic")
    assert http_client.delete(t.url(f"/receipt-layouts/{classic['id']}"), headers=t.owner_headers).status_code == 409
    r = http_client.put(t.url(f"/receipt-layouts/{classic['id']}"), headers=t.owner_headers,
                        json={"is_active": False})
    assert r.status_code == 409


def test_branch_override_roundtrip_and_removal(http_client, pos_tenant):
    t = pos_tenant
    elegant = _by_design(http_client, t, "a4_elegant")
    r = http_client.put(
        t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
        json={"assignments": [{"channel": "whatsapp", "branch_id": t.branch_id, "layout_id": elegant["id"]}]},
    )
    assert_status(r, 200, label="put assignments")
    rows = _assignments(http_client, t)
    assert any(a["channel"] == "whatsapp" and a["branch_id"] == t.branch_id
               and a["layout_id"] == elegant["id"] for a in rows)
    assert len([a for a in rows if a["branch_id"] is None]) == 3  # defaults untouched

    # the branch override wins for that channel only
    sale = _sale(http_client, t)
    wa = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                         params={"channel": "whatsapp"})
    pr = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                         params={"channel": "print"})
    assert wa.json()["layout"]["design"] == "a4_elegant"
    assert pr.json()["layout"]["design"] == "thermal_classic"

    r = http_client.put(
        t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
        json={"assignments": [{"channel": "whatsapp", "branch_id": t.branch_id, "layout_id": None}]},
    )
    assert_status(r, 200, label="remove override")
    wa = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                         params={"channel": "whatsapp"})
    assert wa.json()["layout"]["design"] == "thermal_classic"


def test_business_default_cannot_be_removed_and_layout_must_exist(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.put(t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
                        json={"assignments": [{"channel": "print", "layout_id": None}]})
    assert r.status_code in (400, 409, 422)
    r = http_client.put(
        t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
        json={"assignments": [{"channel": "print", "layout_id": "00000000-0000-4000-8000-000000000001"}]},
    )
    assert r.status_code in (404, 422)
    r = http_client.put(t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
                        json={"assignments": [{"channel": "fax", "layout_id": None}]})
    assert r.status_code == 422


# -- preview ------------------------------------------------------------------


def test_preview_mock_sale(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_classic")
    r = http_client.post(t.url("/receipt-layouts/preview"), headers=t.owner_headers,
                         json={"layout_id": layout["id"]})
    assert_status(r, 200, label="preview")
    body = r.json()
    assert body["mock"] is True
    assert body["model"]["items"] and any(ord(ch) > 0x600 for ch in body["html"])
    assert "<html" in body["html"].lower()


def test_preview_unsaved_config_with_batch_lines(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.post(
        t.url("/receipt-layouts/preview"), headers=t.owner_headers,
        json={"design": "thermal_compact",
              "config": {"v": 1, "items": {"show_batch": True, "show_expiry": True}}},
    )
    assert_status(r, 200, label="preview")
    assert "B2407" in r.json()["text"]
    bad = http_client.post(t.url("/receipt-layouts/preview"), headers=t.owner_headers,
                           json={"design": "thermal_compact", "config": {"v": 1, "nope": 1}})
    assert bad.status_code == 422


def test_preview_real_sale(http_client, pos_tenant):
    t = pos_tenant
    sale = _sale(http_client, t)
    layout = _by_design(http_client, t, "a4_tax_invoice")
    r = http_client.post(t.url("/receipt-layouts/preview"), headers=t.owner_headers,
                         json={"layout_id": layout["id"], "sale_id": sale["id"]})
    assert_status(r, 200, label="preview real")
    body = r.json()
    assert body["mock"] is False
    assert sale["sale_number"] in body["html"]
    assert "300.00" in body["html"]


# -- receipt endpoints use the assigned layout --------------------------------


def test_invoice_receipt_formats_and_public_link_use_the_layout(http_client, pos_tenant):
    t = pos_tenant
    layout = _by_design(http_client, t, "thermal_classic")
    http_client.put(
        t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
        json={"config": {"v": 1, "footer": {"text_en": "CUSTOM-FOOTER-XYZ"}}},
    )
    sale = _sale(http_client, t)
    base = t.url(f"/invoice/{sale['id']}/receipt")
    j = http_client.get(base, headers=t.owner_headers)
    assert_status(j, 200, label="json")
    assert j.json()["footer"]["lines"] and "CUSTOM-FOOTER-XYZ" in str(j.json()["footer"])
    assert "CUSTOM-FOOTER-XYZ" in http_client.get(base, headers=t.owner_headers,
                                                  params={"format": "html"}).text
    assert "CUSTOM-FOOTER-XYZ" in http_client.get(base, headers=t.owner_headers,
                                                  params={"format": "text"}).text
    pdf = http_client.get(base, headers=t.owner_headers, params={"format": "pdf"})
    assert pdf.content[:4] == b"%PDF"
    assert http_client.get(t.url(f"/invoice/{sale['id']}/thermal"), headers=t.owner_headers
                           ).json()["receipt"]["layout"]["design"] == "thermal_classic"

    link = http_client.post(t.url(f"/sales/{sale['id']}/receipt-link"), headers=t.owner_headers).json()
    page = http_client.get(link["url"])
    assert page.status_code == 200
    assert "CUSTOM-FOOTER-XYZ" in page.text
    assert sale["sale_number"] in page.text or "pending" not in page.text.lower()
    assert "pending" not in page.text.lower()


# -- sync feed ----------------------------------------------------------------


def test_feed_carries_layouts_and_assignments(http_client, pos_tenant):
    t = pos_tenant
    changes, cursor, _ = snapshot_all(http_client, t)
    assert {c["data"]["design"] for c in by_entity(changes, "receipt_layout")} == DESIGNS
    assigned = by_entity(changes, "receipt_layout_assignment")
    assert len(assigned) == 3 and all(c["data"]["branch_id"] is None for c in assigned)

    layout = _by_design(http_client, t, "thermal_compact")
    http_client.put(t.url(f"/receipt-layouts/{layout['id']}"), headers=t.owner_headers,
                    json={"name": "Feed rename"})
    elegant = _by_design(http_client, t, "a4_elegant")
    http_client.put(
        t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
        json={"assignments": [{"channel": "link", "branch_id": t.branch_id, "layout_id": elegant["id"]}]},
    )
    feed, _ = pull_all(http_client, t, since=cursor)
    renamed = [c for c in by_entity(feed, "receipt_layout") if c["id"] == layout["id"]]
    assert renamed and renamed[-1]["data"]["name"] == "Feed rename"
    assert any(c["data"]["branch_id"] == t.branch_id for c in by_entity(feed, "receipt_layout_assignment"))

    # remove the override -> tombstone
    http_client.put(
        t.url("/receipt-layouts/assignments"), headers=t.owner_headers,
        json={"assignments": [{"channel": "link", "branch_id": t.branch_id, "layout_id": None}]},
    )
    feed2, _ = pull_all(http_client, t, since=cursor)
    assert any(c["op"] == "delete" for c in by_entity(feed2, "receipt_layout_assignment"))


# -- NTN regression -----------------------------------------------------------


def test_business_tax_id_rejects_tax_labels_and_prints_with_ntn_label(http_client, pos_tenant):
    t = pos_tenant
    for bad in ("GST 17%", "17%", "VAT 5"):
        r = http_client.put(t.url("/business/me"), headers=t.owner_headers, json={"tax_id": bad})
        assert r.status_code == 422, bad
    ok = http_client.put(t.url("/business/me"), headers=t.owner_headers, json={"tax_id": "1234567-8"})
    assert_status(ok, 200, label="tax id")
    sale = _sale(http_client, t)
    text = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                           params={"format": "text"}).text
    assert "NTN: 1234567-8" in text and "GST 17" not in text
    # a legacy bad value already stored is never printed
    from tests.helpers.db import db_execute
    db_execute("UPDATE businesses SET tax_id = 'GST 17%' WHERE id = :b", {"b": t.business_id})
    text = http_client.get(t.url(f"/invoice/{sale['id']}/receipt"), headers=t.owner_headers,
                           params={"format": "text"}).text
    assert "NTN: GST" not in text and "17%" not in text.split("\n")[1:6].__str__()
