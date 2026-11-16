"""Generate a Fernet key for PLATFORM_ENCRYPTION_KEY to encrypt platform secrets at rest.

    python scripts/generate_platform_key.py

Put this key in the server environment (e.g. .env, Railway variables, Kubernetes secret).
Never commit it. Losing this key means stored TOTP secrets cannot be decrypted.
"""

from __future__ import annotations

from cryptography.fernet import Fernet


def main() -> int:
    key = Fernet.generate_key().decode()
    print("# Add to your .env or server environment:")
    print(f"PLATFORM_ENCRYPTION_KEY={key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
