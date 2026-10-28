"""Shared HTTP assertion helpers for integration tests."""

from __future__ import annotations

import httpx


def assert_status(
    response: httpx.Response,
    expected: int,
    *,
    label: str = "",
) -> None:
    prefix = f"{label}: " if label else ""
    detail = ""
    if response.status_code != expected:
        try:
            detail = f" body={response.json()}"
        except Exception:
            detail = f" text={response.text[:400]}"
    assert response.status_code == expected, (
        f"{prefix}HTTP {response.status_code} (expected {expected}){detail}"
    )
