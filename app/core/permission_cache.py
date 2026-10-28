"""Short-lived per-user permission cache.

Keys are ``(user_id, role_version)``. ``role_version`` is a process-wide counter
bumped by ``invalidate_all()`` whenever roles, role permissions or user-role
assignments change in this process; the TTL bounds staleness for changes made
by other workers.
"""

import time
from uuid import UUID

_TTL_SECONDS = 15.0
_MAX_ENTRIES = 5000

_role_version = 0
_store: dict[tuple[UUID, int], tuple[float, frozenset[str]]] = {}


def role_version() -> int:
    return _role_version


def get(user_id: UUID) -> frozenset[str] | None:
    entry = _store.get((user_id, _role_version))
    if entry is None:
        return None
    expires_at, keys = entry
    if expires_at < time.monotonic():
        _store.pop((user_id, _role_version), None)
        return None
    return keys


def put(user_id: UUID, keys: frozenset[str]) -> None:
    if len(_store) >= _MAX_ENTRIES:
        _store.clear()
    _store[(user_id, _role_version)] = (time.monotonic() + _TTL_SECONDS, keys)


def invalidate_all() -> None:
    global _role_version
    _role_version += 1
    _store.clear()
