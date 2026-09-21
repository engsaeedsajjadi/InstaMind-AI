"""Cryptographic primitives used across the platform.

Design rules (see SECURITY.md):

* Passwords are hashed with Argon2id — never stored, never reversible.
* Third-party credentials (Meta access tokens, payment keys) are encrypted at
  rest with Fernet (AES-128-CBC + HMAC-SHA256). The Fernet key is derived from
  ``INSTAMIND_TOKEN_ENCRYPTION_KEY`` and is deliberately *not* the JWT signing
  key, so leaking one does not compromise the other.
* Every ciphertext carries an envelope: ``v1:<key_id>:<fernet_token>`` so keys
  can be rotated without a data migration blackout.
* All comparisons of secrets/mac signatures use ``hmac.compare_digest``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

ENVELOPE_VERSION = "v1"
DEFAULT_KEY_ID = "k1"


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_COST_KIB,
    parallelism=settings.ARGON2_PARALLELISM,
    hash_len=32,
    salt_len=16,
    type=Type.ID,  # Argon2id — OWASP ASVS V2.4
)


def hash_password(password: str) -> str:
    """Return an Argon2id hash string (algorithm+params+salt+digest)."""
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time verification. Never raises for a bad password."""
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: BLE001
        return False


def password_needs_rehash(hashed: str) -> bool:
    """True when stored params are weaker than the current policy."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def generate_password(length: int = 24) -> str:
    """URL-safe random password (used for invited members / bootstrap)."""
    return secrets.token_urlsafe(length)


def password_strength_issues(password: str, min_length: int | None = None) -> list[str]:
    """Return human-readable policy violations. Length is the only hard rule
    enforced by default (NIST SP 800-63B discourages composition rules)."""
    min_length = min_length or settings.PASSWORD_MIN_LENGTH
    issues: list[str] = []
    if len(password) < min_length:
        issues.append(f"password_too_short:{min_length}")
    if password.isalpha() or password.isdigit():
        issues.append("password_low_entropy")
    return issues


# --------------------------------------------------------------------------- #
# Key derivation / encryption at rest
# --------------------------------------------------------------------------- #
def derive_fernet_key(master_secret: str, key_id: str = DEFAULT_KEY_ID, info: bytes = b"token-at-rest") -> bytes:
    """Derive a URL-safe base64 Fernet key from the master secret via HKDF.

    The key_id is mixed into the info label so multiple generations of keys can
    coexist during rotation.
    """
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info + b":" + key_id.encode())
    digest = hkdf.derive(master_secret.encode())
    return base64.urlsafe_b64encode(digest)


class TokenCipher:
    """Encrypt/decrypt opaque secrets at rest with an explicit key registry."""

    def __init__(self, master_secret: str) -> None:
        if not master_secret:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY is not configured. Refusing to run without "
                "encryption for third-party credentials."
            )
        self._master = master_secret
        self._current_key_id = DEFAULT_KEY_ID
        self._keys: dict[str, Fernet] = {
            DEFAULT_KEY_ID: Fernet(derive_fernet_key(master_secret, DEFAULT_KEY_ID))
        }

    def register_key(self, key_id: str, master_secret: str | None = None) -> None:
        """Register an additional generation (for rotation)."""
        self._keys[key_id] = Fernet(
            derive_fernet_key(master_secret or self._master, key_id)
        )

    def set_current_key(self, key_id: str) -> None:
        if key_id not in self._keys:
            raise KeyError(f"unknown key id: {key_id}")
        self._current_key_id = key_id

    # -- api --------------------------------------------------------------- #
    def encrypt(self, plaintext: str, *, aad: str = "") -> str:
        """Encrypt ``plaintext``; optional ``aad`` is bound into the ciphertext.

        ``aad`` should carry the owning record id (e.g. ``"oauth_token:<uuid>"``)
        so a ciphertext copied between rows fails to decrypt.
        """
        token = self._keys[self._current_key_id].encrypt(plaintext.encode("utf-8"))
        envelope = {
            "v": ENVELOPE_VERSION,
            "k": self._current_key_id,
            "t": token.decode("ascii"),
            "aad": aad,
        }
        return base64.urlsafe_b64encode(json.dumps(envelope, separators=(",", ":")).encode()).decode("ascii")

    def decrypt(self, envelope_b64: str, *, aad: str = "") -> str:
        try:
            envelope = json.loads(base64.urlsafe_b64decode(envelope_b64.encode("ascii")))
        except (ValueError, TypeError) as exc:
            raise InvalidCiphertext("malformed ciphertext envelope") from exc
        if envelope.get("v") != ENVELOPE_VERSION:
            raise InvalidCiphertext("unsupported ciphertext version")
        if envelope.get("aad", "") != aad:
            raise InvalidCiphertext("associated data mismatch")
        fernet = self._keys.get(envelope.get("k", ""))
        if fernet is None:
            raise InvalidCiphertext("unknown encryption key generation")
        try:
            return fernet.decrypt(envelope["t"].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise InvalidCiphertext("ciphertext could not be decrypted") from exc


class InvalidCiphertext(Exception):
    """Raised when a stored secret cannot be decrypted."""


@dataclass(frozen=True)
class KeyFingerprint:
    algorithm: str
    key_id: str
    sha256: str


def fingerprint(secret_value: str) -> str:
    """Non-reversible fingerprint for logging / duplicate detection."""
    return hashlib.sha256(secret_value.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def constant_time_equals(a: str | bytes, b: str | bytes) -> bool:
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


def sign_payload(secret: str, payload: bytes) -> str:
    """HMAC-SHA256 hex digest — used for Meta webhook verification."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(secret: str, payload: bytes, signature: str) -> bool:
    expected = sign_payload(secret, payload)
    return constant_time_equals(expected, signature)


def generate_token_id() -> str:
    """Opaque, high-entropy identifier (used for refresh token ids)."""
    return secrets.token_urlsafe(32)


def generate_nonce(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def now_ts() -> float:
    return time.time()


def masked(value: str, keep: int = 4) -> str:
    """Safe representation for logs: never print a raw token."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…{'*' * 8}"


def build_cipher() -> TokenCipher:
    return TokenCipher(settings.TOKEN_ENCRYPTION_KEY.get_secret_value())


_cipher: TokenCipher | None = None


def get_cipher() -> TokenCipher:
    """Process-wide cipher singleton (recreated when tests override settings)."""
    global _cipher
    secret = settings.TOKEN_ENCRYPTION_KEY.get_secret_value()
    if _cipher is None:
        _cipher = TokenCipher(secret)
    return _cipher


def reset_cipher() -> None:
    global _cipher
    _cipher = None


def redact_secrets(payload: Any) -> Any:
    """Best-effort recursive redaction for structured logs."""
    sensitive = {
        "access_token", "appsecret_proof", "refresh_token", "password", "secret",
        "client_secret", "api_key", "authorization", "token", "signature",
    }
    if isinstance(payload, dict):
        return {
            key: ("[redacted]" if key.lower() in sensitive else redact_secrets(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_secrets(item) for item in payload]
    return payload
