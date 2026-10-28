from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_PRODUCTION_SECRET_LENGTH = 32

# Placeholder values shipped in .env.example / older revisions; never valid in production.
_DEFAULT_SECRET_MARKERS = (
    "change-me",
    "changeme",
    "secret",
    "default",
    "example",
    "your-",
    "replace",
)


class ConfigurationError(ValueError):
    """Raised at startup when production settings are unsafe."""


def _looks_default(secret: str) -> bool:
    lowered = secret.lower()
    return any(marker in lowered for marker in _DEFAULT_SECRET_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    env: str = Field(default="development", validation_alias="ENV")
    debug: bool = Field(default=False, validation_alias="DEBUG")

    database_url: str = Field(validation_alias="DATABASE_URL")
    jwt_secret_key: str = Field(validation_alias="JWT_SECRET_KEY")
    jwt_refresh_secret_key: str = Field(validation_alias="JWT_REFRESH_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    access_token_expire_hours: int = Field(
        default=8, validation_alias="ACCESS_TOKEN_EXPIRE_HOURS"
    )
    refresh_token_expire_days: int = Field(
        default=30, validation_alias="REFRESH_TOKEN_EXPIRE_DAYS"
    )
    pin_token_expire_hours: int = Field(
        default=12, validation_alias="PIN_TOKEN_EXPIRE_HOURS"
    )
    max_login_attempts: int = Field(default=5, validation_alias="MAX_LOGIN_ATTEMPTS")
    lockout_minutes: int = Field(default=15, validation_alias="LOCKOUT_MINUTES")
    app_name: str = Field(default="POS SaaS", validation_alias="APP_NAME")
    api_v1_prefix: str = Field(default="/api/v1", validation_alias="API_V1_PREFIX")

    # Comma-separated list of allowed CORS origins.
    allowed_origins: str = Field(
        default="http://localhost:8000,http://localhost:3000",
        validation_alias="ALLOWED_ORIGINS",
    )

    # Request hardening
    max_request_body_bytes: int = Field(
        default=2 * 1024 * 1024, validation_alias="MAX_REQUEST_BODY_BYTES"
    )
    trust_forwarded_for: bool = Field(
        default=False, validation_alias="TRUST_FORWARDED_FOR"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Rate limiting ("<count>/<second|minute|hour|day>"); disable with RATE_LIMIT_ENABLED=false.
    rate_limit_enabled: bool = Field(default=True, validation_alias="RATE_LIMIT_ENABLED")
    rate_limit_login_ip: str = Field(default="10/minute", validation_alias="RATE_LIMIT_LOGIN_IP")
    rate_limit_login_account: str = Field(
        default="10/minute", validation_alias="RATE_LIMIT_LOGIN_ACCOUNT"
    )
    rate_limit_pin_ip: str = Field(default="30/minute", validation_alias="RATE_LIMIT_PIN_IP")
    rate_limit_pin_user: str = Field(default="5/minute", validation_alias="RATE_LIMIT_PIN_USER")
    rate_limit_refresh_ip: str = Field(
        default="30/minute", validation_alias="RATE_LIMIT_REFRESH_IP"
    )
    rate_limit_register_ip: str = Field(
        default="3/hour", validation_alias="RATE_LIMIT_REGISTER_IP"
    )
    rate_limit_pin_change_user: str = Field(
        default="5/minute", validation_alias="RATE_LIMIT_PIN_CHANGE_USER"
    )
    rate_limit_password_change_user: str = Field(
        default="5/minute", validation_alias="RATE_LIMIT_PASSWORD_CHANGE_USER"
    )
    rate_limit_check_alerts_user: str = Field(
        default="6/minute", validation_alias="RATE_LIMIT_CHECK_ALERTS_USER"
    )

    # Optional bot protection for POST /auth/register (disabled by default).
    registration_captcha_enabled: bool = Field(
        default=False, validation_alias="REGISTRATION_CAPTCHA_ENABLED"
    )
    captcha_verify_url: str = Field(
        default="https://challenges.cloudflare.com/turnstile/v0/siteverify",
        validation_alias="CAPTCHA_VERIFY_URL",
    )
    captcha_secret: str = Field(default="", validation_alias="CAPTCHA_SECRET")

    # Password login normally accepts an optional business_slug; set true to require it.
    login_require_business_slug: bool = Field(
        default=False, validation_alias="LOGIN_REQUIRE_BUSINESS_SLUG"
    )

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in {"production", "prod"}

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        if not self.is_production:
            return self

        problems: list[str] = []
        for name, value in (
            ("JWT_SECRET_KEY", self.jwt_secret_key),
            ("JWT_REFRESH_SECRET_KEY", self.jwt_refresh_secret_key),
        ):
            if len(value) < MIN_PRODUCTION_SECRET_LENGTH:
                problems.append(
                    f"{name} must be at least {MIN_PRODUCTION_SECRET_LENGTH} characters"
                )
            elif _looks_default(value):
                problems.append(f"{name} looks like a placeholder/default value")
        if self.jwt_secret_key == self.jwt_refresh_secret_key:
            problems.append("JWT_SECRET_KEY and JWT_REFRESH_SECRET_KEY must differ")
        if not self.cors_origins:
            problems.append("ALLOWED_ORIGINS must be set")
        elif "*" in self.cors_origins:
            problems.append("ALLOWED_ORIGINS must not contain '*'")
        if self.debug:
            problems.append("DEBUG must be false")
        if not self.rate_limit_enabled:
            problems.append("RATE_LIMIT_ENABLED must be true")
        if self.registration_captcha_enabled and not self.captcha_secret:
            problems.append("CAPTCHA_SECRET is required when captcha is enabled")

        if problems:
            raise ConfigurationError(
                "Unsafe production configuration: " + "; ".join(problems)
            )
        return self


settings = Settings()
