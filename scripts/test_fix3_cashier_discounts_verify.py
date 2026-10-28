#!/usr/bin/env python3
"""DEPRECATED (diagnostic only): not migrated to pytest — superseded by tests/integration/permissions/test_misc_permissions.py (test_discounts_view_permissions)."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    print(
        "This script was a print-only diagnostic with no assertions.\n"
        "Use: pytest tests/integration/permissions/test_misc_permissions.py -k discounts_view"
    )
    raise SystemExit(0)
