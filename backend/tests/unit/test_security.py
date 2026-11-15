"""Security primitive tests: password hashing, credential encryption, API-key authentication.

These are deliberately unglamorous. They check the properties that are catastrophic to get
wrong: that a password cannot be recovered from its hash, that two identical passwords produce
different hashes, that a wrong key cannot decrypt a credential, and that a missing API key is
rejected rather than quietly accepted.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    CredentialsNotStorableError,
    ValidationError,
)
from app.core.security import (
    CredentialCipher,
    Principal,
    Role,
    generate_secret,
    get_principal,
    hash_password,
    needs_rehash,
    require_admin,
    verify_password,
)

VALID_SECRET = "a-secret-key-long-enough-to-pass-validation"


# --------------------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------------------


class TestPasswordHashing:
    """Argon2id hashing: one-way, salted, verifiable."""

    def test_hash_verifies(self) -> None:
        hashed = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", hashed)

    def test_wrong_password_does_not_verify(self) -> None:
        hashed = hash_password("correct horse battery staple")
        assert not verify_password("Correct horse battery staple", hashed)

    def test_hash_is_not_the_password(self) -> None:
        password = "correct horse battery staple"
        assert password not in hash_password(password)

    def test_same_password_produces_different_hashes(self) -> None:
        # A per-hash salt is what stops an attacker from cracking a whole table in one pass.
        assert hash_password("same-password") != hash_password("same-password")

    def test_hash_uses_argon2id(self) -> None:
        assert hash_password("some-password").startswith("$argon2id$")

    def test_short_password_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="8 characters"):
            hash_password("short")

    @pytest.mark.parametrize(
        "bad_hash",
        ["", "not-a-hash", "$argon2id$garbage", "$2b$12$abcdefghijklmnopqrstuv"],
    )
    def test_verify_never_raises_on_a_broken_hash(self, bad_hash: str) -> None:
        # A corrupt row must produce a failed login, not a 500.
        assert verify_password("anything", bad_hash) is False

    def test_needs_rehash_is_false_for_a_fresh_hash(self) -> None:
        assert needs_rehash(hash_password("fresh-password")) is False

    def test_needs_rehash_is_true_for_a_broken_hash(self) -> None:
        assert needs_rehash("garbage") is True


# --------------------------------------------------------------------------------------
# Secret generation
# --------------------------------------------------------------------------------------


class TestGenerateSecret:
    """Generated secrets must be unpredictable and long enough."""

    def test_secrets_are_unique(self) -> None:
        assert len({generate_secret() for _ in range(50)}) == 50

    def test_secret_length_is_respected(self) -> None:
        # token_urlsafe(n) returns roughly 4/3 * n characters.
        assert len(generate_secret(32)) >= 32

    def test_too_short_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            generate_secret(4)


# --------------------------------------------------------------------------------------
# Credential encryption
# --------------------------------------------------------------------------------------


class TestCredentialCipher:
    """Proxy credentials are encrypted, not hashed: they must be replayable."""

    def test_round_trip(self) -> None:
        cipher = CredentialCipher("a-long-enough-credential-key-value")
        assert cipher.decrypt(cipher.encrypt("hunter2")) == "hunter2"

    def test_ciphertext_does_not_contain_plaintext(self) -> None:
        cipher = CredentialCipher("a-long-enough-credential-key-value")
        assert "hunter2" not in cipher.encrypt("hunter2")

    def test_encrypting_twice_gives_different_ciphertext(self) -> None:
        # Fernet includes a random IV, so identical inputs differ on the wire.
        cipher = CredentialCipher("a-long-enough-credential-key-value")
        assert cipher.encrypt("same") != cipher.encrypt("same")

    def test_empty_key_is_refused(self) -> None:
        # This is the mechanism behind "refuse rather than store in plain text".
        with pytest.raises(CredentialsNotStorableError):
            CredentialCipher("")

    def test_wrong_key_cannot_decrypt(self) -> None:
        first = CredentialCipher("a-long-enough-credential-key-value")
        second = CredentialCipher("a-completely-different-key-value")
        with pytest.raises(CredentialsNotStorableError, match="could not be decrypted"):
            second.decrypt(first.encrypt("hunter2"))

    def test_corrupt_ciphertext_is_reported_clearly(self) -> None:
        cipher = CredentialCipher("a-long-enough-credential-key-value")
        with pytest.raises(CredentialsNotStorableError):
            cipher.decrypt("not-valid-fernet-ciphertext")

    def test_proper_fernet_key_is_used_unchanged(self) -> None:
        # An operator who already has a Fernet key should not have it re-derived.
        from cryptography.fernet import Fernet

        raw = Fernet.generate_key().decode()
        assert CredentialCipher(raw).decrypt(CredentialCipher(raw).encrypt("x")) == "x"

    def test_unicode_credentials_round_trip(self) -> None:
        cipher = CredentialCipher("a-long-enough-credential-key-value")
        secret = "رمز-عبور-🔐"
        assert cipher.decrypt(cipher.encrypt(secret)) == secret


# --------------------------------------------------------------------------------------
# API authentication
# --------------------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "secret_key": VALID_SECRET,
        "api_key": "the-configured-api-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeRequest:
    """Minimal stand-in for a Starlette request, carrying only what auth touches."""

    def __init__(self) -> None:
        self.state = type("State", (), {})()


class TestApiKeyAuthentication:
    """The API key is the only credential guarding ``/api/v1``."""

    async def test_missing_key_is_rejected(self) -> None:
        with pytest.raises(AuthenticationError, match="X-API-Key"):
            await get_principal(_FakeRequest(), _settings(), None)

    async def test_wrong_key_is_rejected(self) -> None:
        with pytest.raises(AuthenticationError, match="not valid"):
            await get_principal(_FakeRequest(), _settings(), "wrong-key-entirely")

    async def test_correct_key_is_accepted(self) -> None:
        principal = await get_principal(_FakeRequest(), _settings(), "the-configured-api-key")
        assert principal.role is Role.ADMIN
        assert principal.is_admin

    async def test_principal_is_attached_to_the_request(self) -> None:
        # The audit layer reads it from request.state, so this must not be forgotten.
        request = _FakeRequest()
        await get_principal(request, _settings(), "the-configured-api-key")
        assert request.state.principal.role is Role.ADMIN

    async def test_empty_configured_key_yields_a_local_admin(self) -> None:
        # Development convenience. Readiness reports 'degraded' and a warning is logged so this
        # can never be mistaken for a secure configuration.
        request = _FakeRequest()
        principal = await get_principal(request, _settings(api_key=""), None)
        assert principal.is_admin
        assert principal.name == "anonymous-local"

    async def test_empty_configured_key_is_refused_in_production(self) -> None:
        with pytest.raises(AuthenticationError, match="must be configured"):
            await get_principal(
                _FakeRequest(), _settings(api_key="", environment="production"), None
            )

    async def test_key_comparison_is_not_prefix_based(self) -> None:
        # A prefix of the real key must not authenticate.
        with pytest.raises(AuthenticationError):
            await get_principal(_FakeRequest(), _settings(), "the-configured")


class TestRoleEnforcement:
    """``require_admin`` is the authorization gate for state-changing operations."""

    async def test_admin_principal_passes(self) -> None:
        principal = Principal(name="admin", role=Role.ADMIN)
        assert await require_admin(principal) is principal

    async def test_viewer_principal_is_refused(self) -> None:
        with pytest.raises(AuthorizationError, match="admin role"):
            await require_admin(Principal(name="viewer", role=Role.VIEWER))

    def test_admin_property_matches_role(self) -> None:
        assert Principal(name="a", role=Role.ADMIN).is_admin
        assert not Principal(name="v", role=Role.VIEWER).is_admin
