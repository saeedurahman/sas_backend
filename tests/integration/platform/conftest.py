"""Fixtures for the /platform/* suites."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.platform import AdminCtx, new_admin, root_url


@pytest.fixture(scope="session")
def root(api_base: str) -> str:
    return root_url(api_base)


@pytest.fixture(scope="module")
def admin(api_base: str, require_server) -> AdminCtx:
    """A signed-in platform admin, shared by one test module."""
    with httpx.Client(timeout=60.0) as client:
        return new_admin(client, root_url(api_base))
