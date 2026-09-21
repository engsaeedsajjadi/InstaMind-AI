"""Security primitives: password hashing, encryption at rest, signatures."""

from __future__ import annotations

import pytest

from app.core import security
from app.core.security import (
    InvalidCiphertext,
    TokenCipher,
    constant_time_equals,
    fingerprint,
    hash_password,
    masked,
    password_strength_issues,
    redact_secrets,
    sign_payload,
    verify_password,
    verify_signature,
)


class TestPasswords:
    def test_argon2id_is_used_and_verifies(self):
        hashed = hash_password("Correct-Horse-Battery-9")
        assert hashed.startswith("$argon2id$"), "must be Argon2id per OWASP ASVS V2.4"
        assert verify_password("Correct-Horse-Battery-9", hashed) is True

    def test_wrong_password_is_rejected_without_raising(self):
        hashed = hash_password("Correct-Horse-Battery-9")
        assert verify_password("wrong-password-entirely", hashed) is False

    def test_hash_is_salted(self):
        a = hash_password("same-password-value")
        b = hash_password("same-password-value")
        assert a != b, "identical passwords must produce different hashes"

    def test_malformed_hash_returns_false(self):
        assert verify_password("whatever", "not-a-valid-hash") is False

    def test_strength_policy(self):
        assert password_strength_issues("short") != []
        assert password_strength_issues("aaaaaaaaaaaaaaaa") != []  # low entropy
        assert password_strength_issues("Correct-Horse-Battery-9") == []


class TestTokenCipher:
    def test_roundtrip_with_aad(self):
        cipher = TokenCipher("master-secret-value-for-tests")
        blob = cipher.encrypt("IGQV-secret-token", aad="oauth_token:row-1")
        assert "IGQV-secret-token" not in blob
        assert cipher.decrypt(blob, aad="oauth_token:row-1") == "IGQV-secret-token"

    def test_ciphertext_is_bound_to_its_aad(self):
        """Copying a ciphertext to another row must not decrypt."""
        cipher = TokenCipher("master-secret-value-for-tests")
        blob = cipher.encrypt("IGQV-secret-token", aad="oauth_token:row-1")
        with pytest.raises(InvalidCiphertext):
            cipher.decrypt(blob, aad="oauth_token:row-2")

    def test_tampered_ciphertext_is_rejected(self):
        cipher = TokenCipher("master-secret-value-for-tests")
        blob = cipher.encrypt("token", aad="x")
        with pytest.raises(InvalidCiphertext):
            cipher.decrypt(blob[:-4] + "AAAA", aad="x")

    def test_unknown_key_generation_is_rejected(self):
        cipher = TokenCipher("master-secret-value-for-tests")
        blob = cipher.encrypt("token", aad="x")
        other = TokenCipher("a-completely-different-secret")
        with pytest.raises(InvalidCiphertext):
            other.decrypt(blob, aad="x")

    def test_key_rotation_keeps_old_data_readable(self):
        cipher = TokenCipher("master-secret-value-for-tests")
        old = cipher.encrypt("legacy-token", aad="row")
        cipher.register_key("k2")
        cipher.set_current_key("k2")
        new = cipher.encrypt("fresh-token", aad="row")
        assert cipher.decrypt(old, aad="row") == "legacy-token"
        assert cipher.decrypt(new, aad="row") == "fresh-token"

    def test_missing_master_secret_refuses_to_run(self):
        with pytest.raises(ValueError):
            TokenCipher("")


class TestSignatures:
    def test_hmac_roundtrip(self):
        payload = b'{"object":"instagram"}'
        signature = sign_payload("app-secret", payload)
        assert verify_signature("app-secret", payload, signature) is True

    def test_wrong_secret_fails(self):
        payload = b'{"object":"instagram"}'
        signature = sign_payload("app-secret", payload)
        assert verify_signature("other-secret", payload, signature) is False

    def test_tampered_payload_fails(self):
        signature = sign_payload("app-secret", b"original")
        assert verify_signature("app-secret", b"tampered", signature) is False

    def test_constant_time_equals_handles_bytes_and_str(self):
        assert constant_time_equals("abc", b"abc") is True
        assert constant_time_equals("abc", b"abd") is False


class TestLogHygiene:
    def test_tokens_are_redacted_recursively(self):
        payload = {
            "user": {"email": "a@b.c"},
            "auth": {"access_token": "secret", "nested": {"password": "hunter2"}},
            "items": [{"token": "x"}],
        }
        result = redact_secrets(payload)
        assert result["user"]["email"] == "a@b.c"
        assert result["auth"]["access_token"] == "[redacted]"
        assert result["auth"]["nested"]["password"] == "[redacted]"
        assert result["items"][0]["token"] == "[redacted]"

    def test_masked_never_returns_the_secret(self):
        assert masked("IGQVabcdefg") != "IGQVabcdefg"
        assert masked("ab") == "**"

    def test_fingerprint_is_stable_and_short(self):
        assert fingerprint("token-a") == fingerprint("token-a")
        assert fingerprint("token-a") != fingerprint("token-b")
        assert len(fingerprint("token-a")) == 16


def test_cipher_singleton_reads_settings():
    cipher = security.get_cipher()
    blob = cipher.encrypt("value", aad="a")
    assert cipher.decrypt(blob, aad="a") == "value"
