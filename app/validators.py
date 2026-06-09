import re


def validate_phone(value: str) -> str:
    """Validate and normalize phone numbers (optional leading +)."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Phone number is required")

    digits = cleaned[1:] if cleaned.startswith("+") else cleaned
    if not digits.isdigit():
        raise ValueError(
            "Phone number must contain only digits after an optional leading '+'"
        )
    if len(digits) < 10 or len(digits) > 15:
        raise ValueError(
            "Phone number must be between 10 and 15 digits (excluding '+')"
        )
    return cleaned


def validate_password(value: str) -> str:
    """Validate password strength rules."""
    missing: list[str] = []
    if len(value) < 8:
        missing.append("at least 8 characters")
    if not re.search(r"[A-Z]", value):
        missing.append("at least 1 uppercase letter (A-Z)")
    if not re.search(r"[a-z]", value):
        missing.append("at least 1 lowercase letter (a-z)")
    if not re.search(r"[0-9]", value):
        missing.append("at least 1 number (0-9)")
    if missing:
        raise ValueError(f"Password must have {', '.join(missing)}")
    return value


def validate_pin(value: str) -> str:
    """Validate POS PIN (4-6 digits)."""
    cleaned = value.strip()
    if not re.fullmatch(r"\d{4,6}", cleaned):
        raise ValueError("PIN must be 4 to 6 digits only")
    return cleaned
