"""Boolean assertion helper matching original script record() checks."""

from __future__ import annotations


def assert_ok(name: str, ok: bool, detail: str = "") -> None:
    msg = f"{name}"
    if detail:
        msg = f"{msg}: {detail}"
    assert ok, msg
