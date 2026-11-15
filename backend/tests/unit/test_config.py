"""Configuration tests.

Configuration is the first thing that can silently be wrong, and the failure mode is nasty:
an application that starts happily with no API key, or a "development" database URL in
production. These tests pin down the fail-fast behaviour.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import DatabaseSettings, Profile, Settings, get_settings

VALID_SECRET = "a-secret-key-long-enough-to-pass-validation"
VALID_API_KEY = "an-api-key-of-sufficient-length"


def _settings(**overrides: object) -> Settings:
    """Build settings with valid defaults, ignoring any local ``.env`` file."""
    base: dict[str, object] = {
        "_env_file": None,
        "secret_key": VALID_SECRET,
        "api_key": VALID_API_KEY,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestDefaults:
    """The local profile must work with no infrastructure at all."""

    def test_local_profile_defaults_to_sqlite(self) -> None:
        settings = _settings()
        assert settings.profile is Profile.LOCAL
        assert settings.database.is_sqlite
        assert settings.database.url.startswith("sqlite+aiosqlite")

    def test_bind_address_defaults_to_loopback(self) -> None:
        # If this ever defaults to 0.0.0.0 the API becomes reachable from the local network.
        assert _settings().api.host == "127.0.0.1"

    def test_safety_defaults_are_conservative(self) -> None:
        network = _settings().network
        assert network.allow_private_targets is False
        assert network.allowed_target_hosts == []
        assert network.max_concurrent_checks <= 50

    def test_default_mail_backend_is_in_memory(self) -> None:
        assert _settings().mail.backend == "memory"

    def test_default_job_backend_is_in_process(self) -> None:
        assert _settings().jobs.backend == "inprocess"


class TestSecretKeyValidation:
    """A missing or placeholder signing key must abort startup."""

    def test_empty_secret_key_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            _settings(secret_key="")

    def test_placeholder_secret_key_is_rejected(self) -> None:
        # The value shipped in .env.example must never be usable as-is.
        with pytest.raises(PydanticValidationError):
            _settings(secret_key="change-me-please-change-me-please")

    def test_short_secret_key_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            _settings(secret_key="too-short")

    def test_adequate_secret_key_is_accepted(self) -> None:
        assert _settings(secret_key=VALID_SECRET).secret_key == VALID_SECRET


class TestApiKeyValidation:
    """An empty API key is tolerated for local work, but a weak one is not."""

    def test_short_api_key_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            _settings(api_key="short")

    def test_empty_api_key_is_allowed_for_development(self) -> None:
        # Allowed deliberately: the local profile must be usable without ceremony. The
        # application logs a warning and readiness reports 'degraded' instead.
        assert _settings(api_key="").api_key == ""


class TestProductionSafety:
    """Development-only shortcuts must be refused in production."""

    def test_production_requires_api_key(self) -> None:
        settings = _settings(environment="production", api_key="")
        with pytest.raises(ValueError, match="NEXUS_API_KEY"):
            settings.enforce_production_safety()

    def test_production_refuses_relaxed_ssrf_guard(self) -> None:
        settings = _settings(
            environment="production",
            network={
                "allow_private_targets": True,
                "allowed_target_hosts": [],
                "http_timeout_seconds": 5.0,
                "max_concurrent_checks": 2,
                "max_response_bytes": 65536,
            },
        )
        with pytest.raises(ValueError, match="ALLOW_PRIVATE_TARGETS"):
            settings.enforce_production_safety()

    def test_production_refuses_wildcard_cors(self) -> None:
        settings = _settings(environment="production", api={"cors_origins": ["*"]})
        with pytest.raises(ValueError, match="wildcard CORS"):
            settings.enforce_production_safety()

    def test_development_is_not_policed(self) -> None:
        # The escape hatch must stay usable during development, or people will disable the check.
        _settings(network={"allow_private_targets": True}).enforce_production_safety()

    def test_valid_production_configuration_passes(self) -> None:
        _settings(environment="production").enforce_production_safety()


class TestDatabaseUrlHandling:
    """The async URL must be convertible to the sync form Alembic needs."""

    def test_sqlite_async_url_becomes_sync(self) -> None:
        assert (
            DatabaseSettings(url="sqlite+aiosqlite:///./nexus.db").sync_url
            == "sqlite:///./nexus.db"
        )

    def test_postgres_async_url_becomes_sync(self) -> None:
        settings = DatabaseSettings(url="postgresql+asyncpg://u:p@db:5432/nexus")
        assert settings.sync_url == "postgresql+psycopg://u:p@db:5432/nexus"

    def test_sync_url_is_left_alone(self) -> None:
        assert DatabaseSettings(url="sqlite:///./x.db").sync_url == "sqlite:///./x.db"

    def test_is_sqlite_detects_dialect(self) -> None:
        assert DatabaseSettings(url="sqlite+aiosqlite:///:memory:").is_sqlite
        assert not DatabaseSettings(url="postgresql+asyncpg://h/db").is_sqlite


class TestSettingsSingleton:
    """``get_settings`` must be cached, and clearable."""

    def test_singleton_is_cached(self) -> None:
        assert get_settings() is get_settings()

    def test_cache_clear_produces_a_new_object(self) -> None:
        first = get_settings()
        get_settings.cache_clear()
        assert get_settings() is not first


class TestNestedEnvironmentOverrides:
    """Nested ``NEXUS_GROUP__FIELD`` variables must map onto the settings tree."""

    def test_nested_override_is_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE__URL", "sqlite+aiosqlite:///./overridden.db")
        monkeypatch.setenv("NEXUS_NETWORK__MAX_CONCURRENT_CHECKS", "3")
        monkeypatch.setenv("NEXUS_SECRET_KEY", VALID_SECRET)
        settings = Settings(_env_file=None)
        assert settings.database.url.endswith("overridden.db")
        assert settings.network.max_concurrent_checks == 3

    def test_list_setting_parses_json_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_SECRET_KEY", VALID_SECRET)
        monkeypatch.setenv(
            "NEXUS_NETWORK__ALLOWED_TARGET_HOSTS", '["app.example.test", "staging.example.test"]'
        )
        settings = Settings(_env_file=None)
        assert settings.network.allowed_target_hosts == [
            "app.example.test",
            "staging.example.test",
        ]
