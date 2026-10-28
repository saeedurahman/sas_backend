"""/platform/licenses and the public signed revocation list."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from app.licensing import keys
from tests.helpers.db import db_scalar
from tests.helpers.platform import audit_rows
from tests.helpers.server import TEST_LICENSE_PUBLIC_KEY

pytestmark = pytest.mark.integration

PUBLIC = TEST_LICENSE_PUBLIC_KEY


def _new(client, admin, **over):
    body = {
        "customer_name": "Hassan Traders",
        "phone": "0300-1234567",
        "email": "hassan@example.pk",
        "machine_id": f"MID-{uuid.uuid4().hex[:10].upper()}",
        "updates_until": (date.today() + timedelta(days=365)).isoformat(),
    }
    body.update(over)
    r = client.post(admin.url("/licenses"), headers=admin.headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _list(client, admin, **params):
    return client.get(admin.url("/licenses"), headers=admin.headers, params=params).json()


def test_issue_license_returns_a_verifiable_key(http_client, admin):
    lic = _new(http_client, admin, notes="paid cash")
    assert set(lic) == {
        "id", "license_id", "customer_name", "phone", "email", "edition", "machine_id", "issued_at",
        "updates_until", "status", "transfer_count", "previous_license_id", "revoked_at", "notes", "key_string",
    }
    assert lic["status"] == "issued" and lic["edition"] == "local" and lic["transfer_count"] == 0
    assert lic["license_id"].startswith("LIC-") and lic["key_string"].startswith("PPOS-")

    payload = keys.verify_license(lic["key_string"], PUBLIC, machine_id=lic["machine_id"])
    assert payload.as_dict() == {
        "v": 1,
        "license_id": lic["license_id"],
        "edition": "local",
        "customer": "Hassan Traders",
        "machine_id": lic["machine_id"],
        "issued_at": payload.issued_at,
        "updates_until": lic["updates_until"],
    }
    assert payload.issued_at.endswith("Z")
    with pytest.raises(keys.WrongMachine):
        keys.verify_license(lic["key_string"], PUBLIC, machine_id="SOME-OTHER-PC")

    row = audit_rows("license.issue", entity_id=lic["id"])[0]
    assert row["after"]["machine_id"] == lic["machine_id"] and row["admin_email"] == admin.email
    assert "PPOS-" not in json.dumps(row["after"])  # the key itself is not copied into the log


def test_issue_validation(http_client, admin):
    base = {"customer_name": "X", "machine_id": "MID-123", "updates_until": "2099-01-01"}
    url, h = admin.url("/licenses"), admin.headers
    assert http_client.post(url, headers=h, json={**base, "customer_name": ""}).status_code == 422
    assert http_client.post(url, headers=h, json={**base, "machine_id": "ab"}).status_code == 422
    assert http_client.post(url, headers=h, json={**base, "updates_until": "soon"}).status_code == 422
    assert http_client.post(url, headers=h, json={**base, "updates_until": "2000-01-01"}).status_code == 400
    no_date = {k: v for k, v in base.items() if k != "updates_until"}
    assert http_client.post(url, headers=h, json=no_date).status_code == 422


def test_get_one_and_unknown(http_client, admin):
    lic = _new(http_client, admin)
    got = http_client.get(admin.url(f"/licenses/{lic['id']}"), headers=admin.headers)
    assert got.status_code == 200 and got.json() == lic
    assert http_client.get(admin.url(f"/licenses/{uuid.uuid4()}"), headers=admin.headers).status_code == 404


def test_list_search_filter_and_paging(http_client, admin):
    tag = uuid.uuid4().hex[:8]
    a = _new(http_client, admin, customer_name=f"Alpha {tag}")
    b = _new(http_client, admin, customer_name=f"Beta {tag}", phone="0333-9990001")
    c = _new(http_client, admin, customer_name=f"Gamma {tag}")
    http_client.post(admin.url(f"/licenses/{c['id']}/revoke"), headers=admin.headers)

    page = _list(http_client, admin, search=tag)
    assert set(page) == {"items", "total", "page", "page_size"} and page["total"] == 3
    assert [i["id"] for i in _list(http_client, admin, search=f"Alpha {tag}")["items"]] == [a["id"]]
    assert [i["id"] for i in _list(http_client, admin, search="0333-9990001")["items"]][:1] == [b["id"]]
    assert [i["id"] for i in _list(http_client, admin, search=a["machine_id"])["items"]] == [a["id"]]
    assert [i["id"] for i in _list(http_client, admin, search=a["license_id"])["items"]] == [a["id"]]
    revoked = _list(http_client, admin, search=tag, status="revoked")
    assert [i["id"] for i in revoked["items"]] == [c["id"]]
    assert _list(http_client, admin, search=tag, status="issued")["total"] == 2
    p1 = _list(http_client, admin, search=tag, page_size=2, page=1)
    p2 = _list(http_client, admin, search=tag, page_size=2, page=2)
    assert (len(p1["items"]), len(p2["items"])) == (2, 1)
    assert http_client.get(admin.url("/licenses"), headers=admin.headers, params={"status": "bogus"}).status_code == 422


def test_license_file_download(http_client, admin):
    lic = _new(http_client, admin)
    r = http_client.get(admin.url(f"/licenses/{lic['id']}/file"), headers=admin.headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert 'filename="license.txt"' in r.headers["content-disposition"]
    text = r.text
    assert lic["key_string"] in text and lic["license_id"] in text and lic["machine_id"] in text
    assert lic["key_string"] in text.splitlines()  # on its own line, easy to paste


def test_revoke(http_client, admin):
    lic = _new(http_client, admin)
    r = http_client.post(admin.url(f"/licenses/{lic['id']}/revoke"), headers=admin.headers)
    assert r.status_code == 200 and r.json()["status"] == "revoked" and r.json()["revoked_at"]
    again = http_client.post(admin.url(f"/licenses/{lic['id']}/revoke"), headers=admin.headers)
    assert again.status_code == 409
    row = audit_rows("license.revoke", entity_id=lic["id"])[0]
    assert row["before"]["status"] == "issued" and row["after"]["status"] == "revoked"


def test_transfer_revokes_the_old_license_and_issues_a_new_one(http_client, admin):
    old = _new(http_client, admin)
    new_machine = f"NEWPC-{uuid.uuid4().hex[:8].upper()}"
    r = http_client.post(admin.url(f"/licenses/{old['id']}/transfer"), headers=admin.headers,
                         json={"machine_id": new_machine, "notes": "laptop replaced"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"old", "new"}
    assert out["old"]["id"] == old["id"] and out["old"]["status"] == "revoked"
    new = out["new"]
    assert new["id"] != old["id"] and new["license_id"] != old["license_id"]
    assert new["machine_id"] == new_machine and new["transfer_count"] == 1
    assert new["previous_license_id"] == old["id"] and new["status"] == "issued"
    assert new["customer_name"] == old["customer_name"] and new["updates_until"] == old["updates_until"]
    assert new["notes"] == "laptop replaced"

    keys.verify_license(new["key_string"], PUBLIC, machine_id=new_machine)
    with pytest.raises(keys.WrongMachine):
        keys.verify_license(new["key_string"], PUBLIC, machine_id=old["machine_id"])

    # chained transfers keep counting
    again = http_client.post(admin.url(f"/licenses/{new['id']}/transfer"), headers=admin.headers,
                             json={"machine_id": "THIRD-PC-001"}).json()
    assert again["new"]["transfer_count"] == 2

    revoked = set(keys.verify_revocation_list(
        http_client.get(f"{admin.root}/licenses/revocations").json(), PUBLIC))
    assert {old["license_id"], new["license_id"]} <= revoked
    assert again["new"]["license_id"] not in revoked
    assert audit_rows("license.transfer", entity_id=new["id"])[0]["after"]["replaces"] == old["license_id"]


def test_transfer_rules(http_client, admin):
    lic = _new(http_client, admin)
    url = admin.url(f"/licenses/{lic['id']}/transfer")
    assert http_client.post(url, headers=admin.headers, json={"machine_id": lic["machine_id"].lower()}).status_code == 400
    assert http_client.post(url, headers=admin.headers, json={"machine_id": "x"}).status_code == 422
    http_client.post(admin.url(f"/licenses/{lic['id']}/revoke"), headers=admin.headers)
    assert http_client.post(url, headers=admin.headers, json={"machine_id": "BRAND-NEW-PC"}).status_code == 409
    assert http_client.post(admin.url(f"/licenses/{uuid.uuid4()}/transfer"), headers=admin.headers,
                            json={"machine_id": "BRAND-NEW-PC"}).status_code == 404


def test_extend_updates_resigns_the_key(http_client, admin):
    lic = _new(http_client, admin, updates_until=(date.today() + timedelta(days=30)).isoformat())
    later = (date.today() + timedelta(days=400)).isoformat()
    r = http_client.post(admin.url(f"/licenses/{lic['id']}/extend-updates"), headers=admin.headers,
                         json={"updates_until": later})
    assert r.status_code == 200, r.text
    ext = r.json()
    assert ext["updates_until"] == later and ext["license_id"] == lic["license_id"]
    assert ext["key_string"] != lic["key_string"]
    payload = keys.verify_license(ext["key_string"], PUBLIC, machine_id=lic["machine_id"])
    assert payload.updates_until == later and payload.issued_at.endswith("Z")
    row = audit_rows("license.extend_updates", entity_id=lic["id"])[0]
    assert row["before"]["updates_until"] == lic["updates_until"] and row["after"]["updates_until"] == later


def test_extend_rules(http_client, admin):
    lic = _new(http_client, admin)
    url = admin.url(f"/licenses/{lic['id']}/extend-updates")
    for bad in (lic["updates_until"], (date.today()).isoformat()):
        assert http_client.post(url, headers=admin.headers, json={"updates_until": bad}).status_code == 400
    http_client.post(admin.url(f"/licenses/{lic['id']}/revoke"), headers=admin.headers)
    assert http_client.post(url, headers=admin.headers,
                            json={"updates_until": "2099-01-01"}).status_code == 409


# ── public revocation list ────────────────────────────────────────────────────


def test_revocation_list_is_public_signed_and_verifiable(http_client, admin):
    lic = _new(http_client, admin)
    http_client.post(admin.url(f"/licenses/{lic['id']}/revoke"), headers=admin.headers)
    r = http_client.get(f"{admin.root}/licenses/revocations")  # no Authorization header
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    doc = r.json()
    assert set(doc) == {"v", "issued_at", "revoked", "signature"} and doc["v"] == 1
    assert set(doc["revoked"][0]) == {"license_id", "revoked_at"}
    assert lic["license_id"] in keys.verify_revocation_list(doc, PUBLIC)

    # only revoked licenses are listed
    live = _new(http_client, admin)
    assert live["license_id"] not in keys.verify_revocation_list(
        http_client.get(f"{admin.root}/licenses/revocations").json(), PUBLIC)

    # tampering (dropping an entry) or a different key breaks verification
    doc["revoked"] = [e for e in doc["revoked"] if e["license_id"] != lic["license_id"]]
    with pytest.raises(keys.BadSignature):
        keys.verify_revocation_list(doc, PUBLIC)
    other_public = keys.generate_keypair()[1]
    with pytest.raises(keys.BadSignature):
        keys.verify_revocation_list(http_client.get(f"{admin.root}/licenses/revocations").json(), other_public)


def test_a_revoked_key_is_refused_by_the_verifier_using_the_list(http_client, admin):
    lic = _new(http_client, admin)
    http_client.post(admin.url(f"/licenses/{lic['id']}/revoke"), headers=admin.headers)
    revoked = keys.verify_revocation_list(http_client.get(f"{admin.root}/licenses/revocations").json(), PUBLIC)
    with pytest.raises(keys.Revoked):
        keys.verify_license(lic["key_string"], PUBLIC, revoked_ids=revoked)


def test_license_rows_store_the_key_and_status_constraint(http_client, admin):
    lic = _new(http_client, admin)
    assert db_scalar("SELECT key_string FROM licenses WHERE id = :i", {"i": lic["id"]}) == lic["key_string"]
    from tests.helpers.db import db_execute
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db_execute("UPDATE licenses SET status = 'bogus' WHERE id = :i", {"i": lic["id"]})
