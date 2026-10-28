"""Generate the Ed25519 key pair that signs licenses and revocation lists.

    python scripts/generate_license_keypair.py

Put the private key in the server environment ONLY (Railway variable, secret manager);
never commit it. The public key goes into LICENSE_PUBLIC_KEY and is embedded in the
offline edition so it can verify keys without calling home.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.licensing.keys import generate_keypair  # noqa: E402


def main() -> int:
    private_key, public_key = generate_keypair()
    print("# Add to the server environment. Keep the private key secret; it cannot be recovered.")
    print(f"LICENSE_SIGNING_PRIVATE_KEY={private_key}")
    print(f"LICENSE_PUBLIC_KEY={public_key}")
    print("\n# Embed LICENSE_PUBLIC_KEY in the offline app. Losing the private key means no new")
    print("# licenses can be issued; replacing it invalidates every license already issued.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
