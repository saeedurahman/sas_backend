#!/usr/bin/env python3
"""One-time / manual backfill of default role_permissions (supports --dry-run)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.services.role_permission_seed import backfill_role_permissions_for_all_businesses


def _sync_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    return url


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill owner/manager/cashier role_permissions for all businesses.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many role_permissions would be created without writing.",
    )
    args = parser.parse_args()

    engine = create_engine(_sync_database_url(settings.database_url))
    with engine.connect() as connection:
        if args.dry_run:
            result = backfill_role_permissions_for_all_businesses(
                connection, dry_run=True
            )
            connection.rollback()
        else:
            with connection.begin():
                result = backfill_role_permissions_for_all_businesses(
                    connection, dry_run=False
                )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
