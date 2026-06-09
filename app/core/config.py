from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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


settings = Settings()
