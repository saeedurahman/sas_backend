"""License keys and the signed revocation list: sign, verify, tamper, wrong machine."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone

import pytest

from app.licensing import keys

PRIVATE, PUBLIC = keys.generate_keypair()
OTHER_PRIVATE, OTHER_PUBLIC = keys.generate_keypair()
ISSUED = datetime(2026, 10, 29, 8, 15, 0, tzinfo=timezone.utc)


def _payload(**over):
    args = dict(
        license_id="LIC-ABC123",
        customer="Ali Traders",
        machine_id="MACHINE-0001",
        issued_at=ISSUED,
        updates_until=date(2027, 10, 29),
    )
    args.update(over)
    return keys.build_payload(**args)


def _key(**over) -> str:
    return keys.sign_license(_payload(**over), PRIVATE)


def test_keypair_is_raw_32_bytes_base64url_and_matches():
    assert len(keys.b64u_decode(PRIVATE)) == 32
    assert len(keys.b64u_decode(PUBLIC)) == 32
    assert "=" not in PRIVATE + PUBLIC
    assert keys.public_key_for(PRIVATE) == PUBLIC
    assert PRIVATE != OTHER_PRIVATE


def test_key_is_one_pasteable_line_with_prefix():
    key = _key()
    assert key.startswith("PPOS-")
    assert "\n" not in key and " " not in key
    body, sig = key[len("PPOS-"):].split(".")
    assert len(keys.b64u_decode(sig)) == 64  # Ed25519 signature


def test_payload_fields_are_exactly_the_documented_ones():
    key = _key()
    body = keys.b64u_decode(key[len("PPOS-"):].split(".")[0])
    assert json.loads(body) == {
        "v": 1,
        "license_id": "LIC-ABC123",
        "edition": "local",
        "customer": "Ali Traders",
        "machine_id": "MACHINE-0001",
        "issued_at": "2026-10-29T08:15:00Z",
        "updates_until": "2027-10-29",
    }
    # canonical: sorted keys, no whitespace
    assert body == json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()


def test_verify_round_trip():
    payload = keys.verify_license(_key(), PUBLIC)
    assert payload.license_id == "LIC-ABC123"
    assert payload.customer == "Ali Traders"
    assert payload.updates_until_date == date(2027, 10, 29)


def test_signing_is_deterministic_for_the_same_payload():
    assert _key() == _key()


def test_tampered_payload_is_rejected():
    key = _key()
    body_b64, sig_b64 = key[len("PPOS-"):].split(".")
    data = json.loads(keys.b64u_decode(body_b64))
    data["updates_until"] = "2099-01-01"  # try to buy extra updates
    forged_body = keys.b64u_encode(keys.canonical_json(data))
    with pytest.raises(keys.BadSignature):
        keys.verify_license(f"PPOS-{forged_body}.{sig_b64}", PUBLIC)


def test_tampered_signature_is_rejected():
    key = _key()
    body_b64, sig_b64 = key[len("PPOS-"):].split(".")
    raw = bytearray(keys.b64u_decode(sig_b64))
    raw[0] ^= 0x01
    with pytest.raises(keys.BadSignature):
        keys.verify_license(f"PPOS-{body_b64}.{keys.b64u_encode(bytes(raw))}", PUBLIC)


def test_key_signed_by_someone_else_is_rejected():
    forged = keys.sign_license(_payload(), OTHER_PRIVATE)
    with pytest.raises(keys.BadSignature):
        keys.verify_license(forged, PUBLIC)


def test_wrong_machine_is_rejected_and_right_machine_ignores_case_and_spaces():
    key = _key()
    with pytest.raises(keys.WrongMachine):
        keys.verify_license(key, PUBLIC, machine_id="MACHINE-9999")
    assert keys.verify_license(key, PUBLIC, machine_id=" machine-0001 ").machine_id == "MACHINE-0001"


@pytest.mark.parametrize(
    "bad",
    ["", "garbage", "PPOS-", "PPOS-abc", "PPOS-abc.def.ghi", "XXXX-aaaa.bbbb", "PPOS-!!!.???"],
)
def test_malformed_keys_raise_license_error(bad):
    with pytest.raises(keys.LicenseError):
        keys.verify_license(bad, PUBLIC)


def test_unsupported_version_is_rejected():
    bad = keys.LicensePayload(
        v=2, license_id="LIC-1", edition="local", customer="x",
        machine_id="m", issued_at="2026-10-29T00:00:00Z", updates_until="2027-01-01",
    )
    with pytest.raises(keys.UnsupportedVersion):
        keys.verify_license(keys.sign_license(bad, PRIVATE), PUBLIC)


def test_revoked_ids_are_rejected():
    key = _key()
    assert keys.verify_license(key, PUBLIC, revoked_ids={"LIC-OTHER"}).license_id == "LIC-ABC123"
    with pytest.raises(keys.Revoked):
        keys.verify_license(key, PUBLIC, revoked_ids={"LIC-ABC123"})


def test_updates_window():
    payload = keys.verify_license(_key(), PUBLIC)
    assert keys.updates_valid(payload, date(2027, 10, 29))
    assert not keys.updates_valid(payload, date(2027, 10, 30))


def test_machine_id_normalisation():
    assert keys.normalize_machine_id(" ab c-1\t") == "ABC-1"


# ── revocation list ───────────────────────────────────────────────────────────


def _revocations():
    doc = keys.build_revocation_document(
        [("LIC-B", ISSUED), ("LIC-A", ISSUED)], issued_at=ISSUED
    )
    doc["signature"] = keys.sign_document(doc, PRIVATE)
    return doc


def test_revocation_list_round_trip_and_sorted():
    signed = _revocations()
    assert [r["license_id"] for r in signed["revoked"]] == ["LIC-A", "LIC-B"]
    assert keys.verify_revocation_list(signed, PUBLIC) == {"LIC-A", "LIC-B"}


def test_revocation_list_tamper_is_detected():
    signed = _revocations()
    signed["revoked"] = [r for r in signed["revoked"] if r["license_id"] != "LIC-A"]
    with pytest.raises(keys.BadSignature):
        keys.verify_revocation_list(signed, PUBLIC)


def test_revocation_list_signed_by_another_key_is_detected():
    doc = keys.build_revocation_document([], issued_at=ISSUED)
    doc["signature"] = keys.sign_document(doc, OTHER_PRIVATE)
    with pytest.raises(keys.BadSignature):
        keys.verify_revocation_list(doc, PUBLIC)


def test_revocation_signature_survives_json_round_trip():
    signed = json.loads(json.dumps(_revocations()))
    assert keys.verify_revocation_list(signed, PUBLIC)


def test_bad_key_material_is_a_license_error_not_a_crash():
    with pytest.raises(keys.LicenseError):
        keys.sign_license(_payload(), "not-a-key")
    with pytest.raises(keys.LicenseError):
        keys.verify_license(_key(), base64.urlsafe_b64encode(b"short").decode())
