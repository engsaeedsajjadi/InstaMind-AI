#!/usr/bin/env python3
"""Fail-fast production configuration gate.

Usage:
  INSTAMIND_APP_ENV=production ... python scripts/production_gate.py

This is intentionally dependency-free so it can run before the application
boots or before a deployment job starts. It validates configuration contracts,
not external connectivity or Meta App Review state.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    if env("INSTAMIND_APP_ENV", "development") != "production":
        errors.append("INSTAMIND_APP_ENV must be production")

    if env("INSTAMIND_DEBUG", "false").lower() in {"1", "true", "yes", "on"}:
        errors.append("INSTAMIND_DEBUG must be false")

    secret = env("INSTAMIND_SECRET_KEY")
    token_key = env("INSTAMIND_TOKEN_ENCRYPTION_KEY")
    if len(secret) < 32 or secret.startswith("change-me"):
        errors.append("INSTAMIND_SECRET_KEY must be a unique 32+ character secret")
    if len(token_key) < 32 or token_key.startswith("change-me"):
        errors.append("INSTAMIND_TOKEN_ENCRYPTION_KEY must be a unique 32+ character secret")
    if secret and token_key and secret == token_key:
        errors.append("INSTAMIND_SECRET_KEY and INSTAMIND_TOKEN_ENCRYPTION_KEY must differ")

    cors = [item.strip() for item in env("INSTAMIND_CORS_ORIGINS").split(",") if item.strip()]
    if not cors or "*" in cors:
        errors.append("INSTAMIND_CORS_ORIGINS must contain explicit browser origins")
    for origin in cors:
        parsed = urlparse(origin)
        if parsed.scheme != "https":
            warnings.append(f"CORS origin is not HTTPS: {origin}")

    if env("INSTAMIND_STORAGE_BACKEND", "local") != "s3":
        errors.append("Production storage must use INSTAMIND_STORAGE_BACKEND=s3")
    media_url = env("INSTAMIND_PUBLIC_MEDIA_BASE_URL")
    if not media_url.startswith("https://"):
        errors.append("INSTAMIND_PUBLIC_MEDIA_BASE_URL must be an HTTPS URL")
    if not env("INSTAMIND_S3_ENDPOINT_URL"):
        errors.append("INSTAMIND_S3_ENDPOINT_URL is required")
    if not env("INSTAMIND_S3_BUCKET"):
        errors.append("INSTAMIND_S3_BUCKET is required")
    if not env("INSTAMIND_S3_ACCESS_KEY") or not env("INSTAMIND_S3_SECRET_KEY"):
        errors.append("S3 credentials are required in production")

    db = env("INSTAMIND_DATABASE_URL")
    redis = env("INSTAMIND_REDIS_URL")
    if "localhost" in db or "127.0.0.1" in db:
        errors.append("INSTAMIND_DATABASE_URL cannot point to localhost in production")
    if "localhost" in redis or "127.0.0.1" in redis:
        errors.append("INSTAMIND_REDIS_URL cannot point to localhost in production")

    meta_app = env("INSTAMIND_META_APP_ID")
    meta_secret = env("INSTAMIND_META_APP_SECRET")
    if not meta_app or not meta_secret:
        errors.append("Meta app credentials are required")
    if not env("INSTAMIND_META_WEBHOOK_VERIFY_TOKEN"):
        errors.append("INSTAMIND_META_WEBHOOK_VERIFY_TOKEN is required")
    if not (
        env("INSTAMIND_META_INSTAGRAM_CLIENT_ID")
        and env("INSTAMIND_META_INSTAGRAM_CLIENT_SECRET")
    ) and not (
        env("INSTAMIND_META_FACEBOOK_CLIENT_ID")
        and env("INSTAMIND_META_FACEBOOK_CLIENT_SECRET")
    ):
        errors.append("At least one official Meta OAuth path must be configured")

    ai_provider = env("INSTAMIND_AI_PROVIDER", "null")
    if ai_provider == "openai" and not env("INSTAMIND_OPENAI_API_KEY"):
        errors.append("INSTAMIND_OPENAI_API_KEY is required when AI provider is openai")

    billing = env("INSTAMIND_BILLING_PROVIDER", "null")
    if billing == "stripe" and not env("INSTAMIND_STRIPE_SECRET_KEY"):
        errors.append("INSTAMIND_STRIPE_SECRET_KEY is required for Stripe billing")
    if billing == "zarinpal" and not env("INSTAMIND_ZARINPAL_MERCHANT_ID"):
        errors.append("INSTAMIND_ZARINPAL_MERCHANT_ID is required for Zarinpal billing")
    if billing == "null":
        warnings.append("Billing provider is disabled; paid checkout is not available")

    if env("INSTAMIND_EMAIL_BACKEND", "console") != "smtp":
        warnings.append("Email backend is not SMTP; verification/invitations are not production mail")

    if errors:
        print("PRODUCTION GATE: FAILED")
        for item in errors:
            print(f"ERROR: {item}")
        for item in warnings:
            print(f"WARN:  {item}")
        return 1

    print("PRODUCTION GATE: PASSED")
    for item in warnings:
        print(f"WARN:  {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
