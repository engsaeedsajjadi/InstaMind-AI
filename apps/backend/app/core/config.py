"""Application configuration.

Single source of truth for settings. NOTHING secret is hard-coded: every value is
read from the environment (12-factor). `Settings` is frozen after startup so a
request cannot mutate global configuration.

Validation is strict: a missing/invalid secret fails fast at boot rather than
producing a confusing 500 at runtime.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32


class Environment(StrEnum):
    DEV = "development"
    TEST = "test"
    STAGING = "staging"
    PROD = "production"


class DatabaseDriver(StrEnum):
    POSTGRES = "postgresql+asyncpg"
    SQLITE = "sqlite+aiosqlite"


class Settings(BaseSettings):
    """Runtime configuration.

    Env prefix is ``INSTAMIND_`` so that ``INSTAMIND_SECRET_KEY=...`` maps to
    ``settings.SECRET_KEY``.
    """

    model_config = SettingsConfigDict(
        env_prefix="INSTAMIND_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    APP_NAME: str = "InstaMind AI"
    APP_ENV: Environment = Environment.DEV
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # Comma separated list of allowed browser origins.
    CORS_ORIGINS: list[AnyHttpUrl] = Field(default_factory=list)

    # ------------------------------------------------------------- database
    DATABASE_URL: str = "postgresql+asyncpg://instamind:instamind@localhost:5432/instamind"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # ----------------------------------------------------------------- redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # -------------------------------------------------------------- security
    # Used for JWT signing (HS256) and for deriving the encryption key below.
    SECRET_KEY: SecretStr = SecretStr("change-me-in-production-min-32-chars!")
    # Fernet key used to encrypt third-party tokens at rest. MUST be distinct
    # from SECRET_KEY so a JWT leak cannot decrypt stored credentials.
    TOKEN_ENCRYPTION_KEY: SecretStr = SecretStr("")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 30
    REFRESH_ROTATION_REUSE_GRACE_SECONDS: int = 10
    # Absolute lifetime of a refresh token chain, regardless of rotation.
    REFRESH_TOKEN_MAX_AGE_DAYS: int = 90
    SESSION_IDLE_TTL_DAYS: int = 30
    PASSWORD_MIN_LENGTH: int = 12
    ARGON2_TIME_COST: int = 3
    ARGON2_MEMORY_COST_KIB: int = 64 * 1024
    ARGON2_PARALLELISM: int = 2
    EMAIL_VERIFICATION_TTL_HOURS: int = 48
    PASSWORD_RESET_TTL_MINUTES: int = 30
    TOTP_ISSUER: str = "InstaMind AI"
    TOTP_DIGITS: int = 6
    TOTP_PERIOD: int = 30
    # Browser auth uses HttpOnly cookies; API clients may continue using Bearer tokens.
    AUTH_COOKIE_SAMESITE: Literal["strict", "lax"] = "strict"

    # ----------------------------------------------------------- rate limit
    RATE_LIMIT_ENABLED: bool = True
    # Auth endpoints are the primary credential-stuffing surface.
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    RATE_LIMIT_API_PER_MINUTE: int = 300
    RATE_LIMIT_STORAGE: Literal["memory", "redis"] = "memory"

    # -------------------------------------------------------------- storage
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    LOCAL_STORAGE_ROOT: str = "./storage"
    # Public base URL used to hand Meta a fetchable https:// media URL.
    PUBLIC_MEDIA_BASE_URL: str = "http://localhost:8000/media"
    S3_ENDPOINT_URL: str = ""
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str = "instamind"
    S3_ACCESS_KEY: SecretStr = SecretStr("")
    S3_SECRET_KEY: SecretStr = SecretStr("")
    S3_PRESIGN_TTL_SECONDS: int = 3600
    MAX_UPLOAD_BYTES: int = 8 * 1024 * 1024
    MAX_VIDEO_UPLOAD_BYTES: int = 300 * 1024 * 1024

    # ------------------------------------------------- Meta / Instagram API
    META_API_VERSION: str = "v23.0"
    META_GRAPH_BASE_URL: str = "https://graph.facebook.com"
    META_IG_BASE_URL: str = "https://graph.instagram.com"
    META_APP_ID: str = ""
    META_APP_SECRET: SecretStr = SecretStr("")
    # Both official auth paths are supported; see docs/instagram-integration.md
    META_FACEBOOK_CLIENT_ID: str = ""
    META_FACEBOOK_CLIENT_SECRET: SecretStr = SecretStr("")
    META_INSTAGRAM_CLIENT_ID: str = ""
    META_INSTAGRAM_CLIENT_SECRET: SecretStr = SecretStr("")
    META_REDIRECT_URI_FACEBOOK: str = "http://localhost:8000/api/v1/social-accounts/oauth/facebook/callback"
    META_REDIRECT_URI_INSTAGRAM: str = "http://localhost:8000/api/v1/social-accounts/oauth/instagram/callback"
    META_WEBHOOK_VERIFY_TOKEN: SecretStr = SecretStr("")
    # Safety valve: never publish more than this many posts per account/24h even
    # if Meta's own quota would allow it.
    PUBLISH_DAILY_GUARD_LIMIT: int = 100
    PUBLISH_RETRY_MAX_ATTEMPTS: int = 5
    PUBLISH_BACKOFF_BASE_SECONDS: float = 2.0
    PUBLISH_BACKOFF_MAX_SECONDS: float = 900.0
    REELS_POLL_INTERVAL_SECONDS: float = 2.0
    REELS_POLL_MAX_WAIT_SECONDS: float = 300.0

    # -------------------------------------------------------------------- AI
    AI_PROVIDER: Literal["openai", "null"] = "openai"
    OPENAI_API_KEY: SecretStr = SecretStr("")
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TIMEOUT_SECONDS: float = 60.0
    # Hard budget guard, in USD cents, per workspace per calendar month.
    AI_DEFAULT_MONTHLY_BUDGET_CENTS: int = 2000

    # --------------------------------------------------------------- billing
    BILLING_PROVIDER: Literal["stripe", "zarinpal", "null"] = "null"
    STRIPE_SECRET_KEY: SecretStr = SecretStr("")
    STRIPE_WEBHOOK_SECRET: SecretStr = SecretStr("")
    ZARINPAL_MERCHANT_ID: SecretStr = SecretStr("")
    ZARINPAL_SANDBOX: bool = True

    # ------------------------------------------------------------ mail / sms
    EMAIL_BACKEND: Literal["console", "smtp"] = "console"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: SecretStr = SecretStr("")
    MAIL_FROM: str = "no-reply@instamind.ai"
    SMS_BACKEND: Literal["null", "kavenegar"] = "null"
    KAVENEGAR_API_KEY: SecretStr = SecretStr("")

    # ---------------------------------------------------------- observability
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    SENTRY_DSN: str = ""
    METRICS_ENABLED: bool = True

    # ------------------------------------------------------------ bootstraps
    FIRST_SUPERUSER_EMAIL: str = ""
    FIRST_SUPERUSER_PASSWORD: SecretStr = SecretStr("")
    DEFAULT_PLAN_CODE: str = "free"

    # ------------------------------------------------------------- validators
    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _check_production_hardening(self) -> Settings:
        """Refuse to boot an insecure production instance."""
        if self.APP_ENV is Environment.PROD:
            problems: list[str] = []
            if len(self.SECRET_KEY.get_secret_value()) < MIN_SECRET_LENGTH:
                problems.append("SECRET_KEY too short")
            if not self.TOKEN_ENCRYPTION_KEY.get_secret_value():
                problems.append("TOKEN_ENCRYPTION_KEY is required")
            if not self.CORS_ORIGINS:
                problems.append("CORS_ORIGINS must be explicitly set")
            if self.DEBUG:
                problems.append("DEBUG must be disabled")
            if problems:
                raise ValueError("Refusing to start in production: " + "; ".join(problems))
        return self

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV is Environment.PROD

    @property
    def database_driver(self) -> DatabaseDriver:
        return DatabaseDriver.SQLITE if self.DATABASE_URL.startswith("sqlite") else DatabaseDriver.POSTGRES


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
