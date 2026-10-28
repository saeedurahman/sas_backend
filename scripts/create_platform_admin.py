"""Create the first platform admin (the owner who uses the admin web panel).

    python scripts/create_platform_admin.py --email owner@example.com --name "Owner"

The password is read from the PLATFORM_ADMIN_PASSWORD environment variable or prompted
(never passed on the command line). The script prints the TOTP provisioning URI: add it
to an authenticator app (Google Authenticator, Authy, ...) - it is shown only once.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def _create(email: str, name: str, password: str) -> int:
    from fastapi import HTTPException

    from app.database import AsyncSessionLocal, engine
    from app.services import platform_admin_service as admins

    try:
        async with AsyncSessionLocal() as db:
            admin = await admins.create_admin(db, None, email=email, name=name, password=password)
            creds = admins.credentials_for(admin)
    except HTTPException as exc:
        print(f"Error: {exc.detail}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    print(f"Created platform admin {admin.email}")
    print("\nAdd this to your authenticator app (shown once):")
    print(f"  {creds['provisioning_uri']}")
    print(f"\nOr enter the secret manually: {creds['totp_secret']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    password = os.environ.get("PLATFORM_ADMIN_PASSWORD") or getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        print("Error: password must be at least 12 characters", file=sys.stderr)
        return 1
    if "@" not in args.email:
        print("Error: invalid email", file=sys.stderr)
        return 1
    return asyncio.run(_create(args.email, args.name, password))


if __name__ == "__main__":
    raise SystemExit(main())
