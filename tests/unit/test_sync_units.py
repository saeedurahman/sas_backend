"""Pure-logic checks for the sync layer (no database)."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import sync_feed, sync_push

ROOT = Path(__file__).resolve().parents[2]


# ── cursors ──────────────────────────────────────────────────────────────────


def test_cursor_roundtrip_and_tamper_detection():
    token = sync_feed.encode_token({"v": 1, "k": "pull", "s": 42})
    assert sync_feed.decode_token(token)["s"] == 42
    body, signature = token.split(".")
    forged_body = sync_feed._b64(b'{"k":"pull","s":9999,"v":1}')
    for bad in (f"{forged_body}.{signature}", body, "", "not.a.token", f"{body}.AAAA"):
        with pytest.raises(sync_feed.CursorError):
            sync_feed.decode_token(bad)


def test_cursor_is_opaque_and_unversioned_tokens_rejected():
    token = sync_feed.encode_token({"v": 2, "s": 1})
    with pytest.raises(sync_feed.CursorError):
        sync_feed.decode_token(token)
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", sync_feed.encode_token({"v": 1}))


def test_limit_clamped():
    assert sync_feed._clamp(None) == sync_feed.DEFAULT_LIMIT == 500
    assert sync_feed._clamp(0) == sync_feed.DEFAULT_LIMIT  # falsy -> default
    assert sync_feed._clamp(10_000) == sync_feed.MAX_LIMIT
    assert sync_feed._clamp(7) == 7


def test_entity_registry_covers_the_spec():
    names = {e.name for e in sync_feed.ENTITIES}
    assert {
        "product", "product_variation", "category", "brand", "unit", "tax_rate", "price_list",
        "price_list_item", "customer", "discount_scheme", "settings", "branch", "user", "role",
        "stock_level",
    } <= names
    assert len(names) == len(sync_feed.ENTITIES)  # unique
    assert sync_feed.RESTRICTED_ENTITIES == ("settings",)


# ── push helpers ─────────────────────────────────────────────────────────────


def _raw(**overrides):
    base = {
        "op_id": str(uuid.uuid4()),
        "type": "customer.create",
        "client_created_at": "2026-10-29T08:00:00Z",
        "occurred_at": "2026-10-29T08:00:00Z",
        "payload": {"id": str(uuid.uuid4()), "name": "A"},
    }
    base.update(overrides)
    return base


def test_parse_op_accepts_valid_and_normalises_depends_on():
    dep = str(uuid.uuid4())
    op, rejected = sync_push._parse_op(_raw(depends_on=dep))
    assert rejected is None and [str(d) for d in op.depends_on] == [dep]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("nope", "malformed_op"),
        (_raw(op_id="not-a-uuid"), "malformed_op"),
        (_raw(occurred_at="2026-10-29T08:00:00"), "malformed_op"),  # no UTC offset
        (_raw(payload="x"), "malformed_op"),
        (_raw(type="held_order.create"), "unsupported_op_type"),
        (_raw(type="teleport"), "unsupported_op_type"),
    ],
)
def test_parse_op_rejections_have_stable_codes(raw, code):
    op, rejected = sync_push._parse_op(raw)
    assert op is None
    assert rejected["status"] == "rejected" and rejected["error"]["code"] == code
    assert rejected["error"]["retryable"] is False


def test_parse_op_payload_size_limit(monkeypatch):
    monkeypatch.setattr(sync_push.settings, "sync_max_payload_bytes", 1024)
    op, rejected = sync_push._parse_op(_raw(payload={"id": str(uuid.uuid4()), "name": "x" * 2000}))
    assert op is None and rejected["error"]["code"] == "payload_too_large"


def test_payload_hash_ignores_key_order_but_not_content():
    a, _ = sync_push._parse_op(_raw(payload={"a": 1, "b": 2}, type="customer.create"))
    b = a.model_copy(update={"payload": {"b": 2, "a": 1}})
    c = a.model_copy(update={"payload": {"a": 1, "b": 3}})
    assert sync_push._payload_hash(a) == sync_push._payload_hash(b)
    assert sync_push._payload_hash(a) != sync_push._payload_hash(c)


def _ctx(state_status="active", **overrides):
    from app.services import subscription as sub

    now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    state = sub.SubscriptionState(state_status, None, None, None, None, state_status == "grace")
    values = dict(
        db=None, business_id=uuid.uuid4(), user_id=uuid.uuid4(),
        permissions=frozenset(sync_push.OP_PERMISSIONS.values()), branches=None,
        business_state=state, device_id="d", batch_id="b", now=now,
    )
    values.update(overrides)
    return sync_push.Ctx(**values)


def _op_at(delta, op_type="sale.create"):
    ctx = _ctx()
    op, _ = sync_push._parse_op(
        _raw(type=op_type, occurred_at=(ctx.now + delta).strftime("%Y-%m-%dT%H:%M:%SZ"),
             payload={})
    )
    return ctx, op


def test_gate_time_window(monkeypatch):
    monkeypatch.setattr(sync_push.settings, "max_offline_days", 14)
    ctx, op = _op_at(-timedelta(days=13, hours=23))
    sync_push._gate(ctx, op)
    ctx, op = _op_at(-timedelta(days=14, minutes=1))
    with pytest.raises(sync_push.OpReject) as exc:
        sync_push._gate(ctx, op)
    assert exc.value.code == "occurred_too_old" and not exc.value.retryable
    ctx, op = _op_at(timedelta(minutes=4))
    sync_push._gate(ctx, op)
    ctx, op = _op_at(timedelta(minutes=6))
    with pytest.raises(sync_push.OpReject) as exc:
        sync_push._gate(ctx, op)
    assert exc.value.code == "occurred_in_future"


def test_gate_permissions_and_subscription():
    ctx, op = _op_at(timedelta(0), "sale.void")
    sync_push._gate(ctx, op)
    ctx.permissions = frozenset()
    with pytest.raises(sync_push.OpReject) as exc:
        sync_push._gate(ctx, op)
    assert exc.value.code == "permission_denied"

    for status, allowed_code in (("grace", "subscription_grace_readonly"), ("expired", "subscription_expired"),
                                 ("suspended", "subscription_suspended"), ("cancelled", "subscription_cancelled")):
        for op_type in sync_push.LAPSED_ALLOWED:
            ctx, op = _op_at(timedelta(0), op_type)
            ctx.business_state = _ctx(status).business_state
            sync_push._gate(ctx, op)  # money ops always pass
        for op_type in ("customer.create", "expense.create", "shift.open", "sale.void", "return.create"):
            ctx, op = _op_at(timedelta(0), op_type)
            ctx.business_state = _ctx(status).business_state
            with pytest.raises(sync_push.OpReject) as exc:
                sync_push._gate(ctx, op)
            assert exc.value.code == allowed_code and exc.value.retryable


def test_replay_session_turns_commit_into_flush_and_rollback_into_noop():
    calls = []

    class Fake:
        async def flush(self):
            calls.append("flush")

        async def commit(self):
            calls.append("REAL COMMIT")

        async def rollback(self):
            calls.append("REAL ROLLBACK")

        marker = "delegated"

    session = sync_push.ReplaySession(Fake())

    async def run():
        await session.commit()
        await session.rollback()

    asyncio.run(run())
    assert calls == ["flush"] and session.marker == "delegated"


def test_error_codes_are_stable_strings():
    # Documented in the README; renaming one is a breaking API change.
    assert {
        sync_push.E_MALFORMED, sync_push.E_PAYLOAD, sync_push.E_TOO_LARGE, sync_push.E_UNSUPPORTED,
        sync_push.E_TOO_OLD, sync_push.E_FUTURE, sync_push.E_PERMISSION, sync_push.E_UNKNOWN_PRODUCT,
        sync_push.E_DUP_RECEIPT, sync_push.E_REUSED,
    } == {
        "malformed_op", "malformed_payload", "payload_too_large", "unsupported_op_type",
        "occurred_too_old", "occurred_in_future", "permission_denied", "unknown_product",
        "duplicate_receipt", "idempotency_key_reused",
    }
    assert set(sync_push.ALL_FLAGS) == {
        "price_mismatch", "tax_mismatch", "oversold", "credit_limit_exceeded",
        "cost_estimated", "shift_mismatch", "subscription_expired",
    }


# ── tombstones: synced rows are soft-deleted, never hard-deleted ─────────────


def test_synced_tables_are_never_hard_deleted_by_the_app():
    synced_models = {
        "Product", "ProductVariation", "Category", "Brand", "Unit", "TaxRate", "PriceList",
        "PriceListItem", "Customer", "DiscountScheme", "Branch", "CashRegister", "Barcode",
        "User", "Role", "BusinessConfig",
    }
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf8")
        for model in synced_models:
            if re.search(rf"\bdelete\(\s*{model}\b", text) or re.search(rf"db\.delete\(\s*(?:{model.lower()})\b", text):
                offenders.append(f"{path.relative_to(ROOT)}: {model}")
    assert not offenders, offenders


def test_every_synced_table_has_deleted_at():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mig019", ROOT / "alembic" / "versions" / "019_offline_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import app.models  # noqa: F401 - registers every table on the metadata
    from app.models.base import Base as DeclBase

    tables = DeclBase.metadata.tables
    for name in module.SYNCED_TABLES:
        if name == "stock_levels":  # created by the migration itself, has deleted_at there
            continue
        assert "deleted_at" in tables[name].c, f"{name} has no tombstone column"
