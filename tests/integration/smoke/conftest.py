"""Smoke-suite fixtures: fail any test during which the API server hit an async lazy load."""

from __future__ import annotations

import pytest

from tests.helpers.server import SERVER_LOGS, greenlet_errors


@pytest.fixture(autouse=True)
def no_missing_greenlet(request: pytest.FixtureRequest):
    """Fail the test if the (managed) server logged MissingGreenlet while it ran.

    A lazy-loaded relationship touched during response serialization surfaces to the client
    only as a bare 500; the traceback is in the server log. The tests also assert 2xx, this
    makes the cause explicit. (Not available when ``API_BASE`` points at an external server.)
    """
    api_base = request.getfixturevalue("api_base")
    log = SERVER_LOGS.get(api_base)
    start = log.stat().st_size if log is not None and log.exists() else 0
    yield
    if log is not None:
        errors = greenlet_errors(log, start)
        assert not errors, "MissingGreenlet during response serialization:\n" + "\n".join(errors[:5])
