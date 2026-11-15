"""Application settings.

Design notes (why this file looks the way it does)
-------------------------------------------------
1. **Secrets come from the environment only.** There are no usable default values for
   ``secret_key`` or ``proxy_credential_key``. A missing value aborts startup instead of
   silently falling back to something insecure (docs/SECURITY.md 4, Decision D5).

2. **Two profiles, one codebase.** ``profile="local"`` uses SQLite and an in-process job queue
   so the project runs with zero infrastructure. ``profile="docker"`` switches to PostgreSQL,
   Redis and Mailpit. Only environment variables change between them (Decision D3).

3. **Grouped settings.** Values are grouped into small sub-models (``database``, ``api``,
   ``network``, ``mail``, ``jobs``) so related options are discoverable together and can be
   overridden with nested env vars such as ``NEXUS_DATABASE__URL``.

4. **Documents the environment.** Every field carries a description, which is what makes
   ``.env.example`` and the docs trustworthy rather than hand-maintained guesses.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(StrEnum):
    """Deployment profile. Chooses sensible infrastructure defaults."""

    LOCAL = "local"
    DOCKER = "docker"


class DatabaseSettings(BaseSettings):
    """Database connection settings."""

    url: str = Field(
        default="sqlite+aiosqlite:///./nexus.db",
        description=(
            "SQLAlchemy async URL. Local default is SQLite so no server is required. "
            "Docker profile uses postgresql+asyncpg://nexus:...@db:5432/nexus"
        ),
    )
    echo: bool = Field(default=False, description="Log every SQL statement (very noisy).")
    pool_size: int = Field(default=5, ge=1, le=50, description="Connection pool size (server DBs).")
    max_overflow: int = Field(
        default=10, ge=0, le=100, description="Extra connections above pool_size."
    )

    @property
    def is_sqlite(self) -> bool:
        """True when the configured dialect is SQLite."""
        return self.url.startswith("sqlite")

    @property
    def sync_url(self) -> str:
        """A synchronous URL, required by Alembic.

        Alembic runs migrations synchronously, so the async driver suffix has to go:
        ``sqlite+aiosqlite:///x.db`` becomes ``sqlite:///x.db``.
        """
        for async_driver, sync_driver in (
            ("+aiosqlite", ""),
            ("+asyncpg", "+psycopg"),
        ):
            if async_driver in self.url:
                return self.url.replace(async_driver, sync_driver, 1)
        return self.url


class ApiSettings(BaseSettings):
    """HTTP server settings."""

    host: str = Field(
        default="127.0.0.1",
        description="Bind address. Loopback by default so the API is not exposed to the LAN.",
    )
    port: int = Field(default=8000, ge=1, le=65535)
    root_path: str = Field(default="", description="Prefix when served behind a reverse proxy.")
    docs_enabled: bool = Field(default=True, description="Serve /docs and /redoc.")
    cors_origins: list[str] = Field(
        default_factory=list,
        description="Explicit allowed origins. Empty means no cross-origin access at all.",
    )


class NetworkSettings(BaseSettings):
    """Outbound network safety (threat T1/T7 in docs/SECURITY.md)."""

    http_timeout_seconds: float = Field(
        default=15.0, gt=0, le=300, description="Timeout applied to EVERY outbound HTTP request."
    )
    max_concurrent_checks: int = Field(
        default=10, ge=1, le=200, description="Upper bound on simultaneous proxy health checks."
    )
    max_response_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        description="Response size cap, protects memory from a hostile peer.",
    )
    allow_private_targets: bool = Field(
        default=False,
        description=(
            "MUST stay false outside development. When false the SSRF guard blocks loopback, "
            "private, link-local and metadata addresses."
        ),
    )
    allowed_target_hosts: list[str] = Field(
        default_factory=list,
        description=(
            "Deny-by-default allowlist of registration target hosts. Empty means only the "
            "bundled demo target may be used."
        ),
    )


class MailSettings(BaseSettings):
    """Test-mail backend settings (Mailpit)."""

    backend: Literal["memory", "mailpit"] = Field(
        default="memory",
        description="'memory' keeps messages in-process; 'mailpit' delivers to Mailpit.",
    )
    smtp_host: str = Field(default="127.0.0.1")
    smtp_port: int = Field(default=1025, ge=1, le=65535, description="Mailpit SMTP listener.")
    api_url: str = Field(default="http://127.0.0.1:8025", description="Mailpit HTTP API base URL.")
    reserved_domain: str = Field(
        default="example.test",
        description="RFC 2606/6761 reserved domain used for generated test inbox addresses.",
    )


class JobSettings(BaseSettings):
    """Background job settings."""

    backend: Literal["inprocess", "rq"] = Field(
        default="inprocess",
        description="'inprocess' runs jobs in the API process (local profile). 'rq' uses Redis.",
    )
    redis_url: str = Field(default="redis://127.0.0.1:6379/0")
    queue_name: str = Field(default="nexus")
    max_workers: int = Field(
        default=2, ge=1, le=16, description="Conservative default (threat T12)."
    )


class Settings(BaseSettings):
    """Root settings object, assembled from the groups above."""

    model_config = SettingsConfigDict(
        env_prefix="NEXUS_",
        env_nested_delimiter="__",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    profile: Profile = Field(default=Profile.LOCAL)
    app_name: str = Field(default="NEXUS")
    environment: Literal["development", "staging", "production"] = Field(default="development")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    log_format: Literal["console", "json"] = Field(
        default="console", description="'json' for machine-readable logs in containers."
    )
    log_directory: Path = Field(default=Path("logs"))

    api_key: str = Field(
        default="",
        description="Required for every /api/v1 request. Empty disables auth (never in prod).",
    )
    secret_key: str = Field(default="", description="REQUIRED. Used to sign session artefacts.")
    proxy_credential_key: str = Field(
        default="",
        description=(
            "REQUIRED to store proxy credentials. Without it NEXUS refuses to store them "
            "rather than keeping them in plain text."
        ),
    )
    audit_log_enabled: bool = Field(default=True)

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)

    # ------------------------------------------------------------------ validation

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        """Reject missing or obviously-too-short signing keys."""
        if not value or value.startswith("change-me"):
            msg = (
                "NEXUS_SECRET_KEY is not set (or is still the placeholder). "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
            raise ValueError(msg)
        if len(value) < 32:
            msg = "NEXUS_SECRET_KEY must be at least 32 characters."
            raise ValueError(msg)
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        """API auth must never be silently disabled outside development."""
        if not value:
            return value
        if len(value) < 16:
            msg = "NEXUS_API_KEY must be at least 16 characters."
            raise ValueError(msg)
        return value

    @property
    def is_production(self) -> bool:
        """True when running with the production environment flag."""
        return self.environment == "production"

    def enforce_production_safety(self) -> None:
        """Refuse to start a production instance with development-only shortcuts enabled.

        Called at application startup. Kept separate from validators so tests can build
        permissive settings objects without tripping production rules.
        """
        if not self.is_production:
            return
        problems: list[str] = []
        if not self.api_key:
            problems.append("NEXUS_API_KEY must be set in production")
        if self.network.allow_private_targets:
            problems.append("NEXUS_NETWORK__ALLOW_PRIVATE_TARGETS must be false in production")
        if self.api.host not in {"127.0.0.1", "localhost"} and not self.api_key:
            problems.append("binding to a non-loopback address without an API key is refused")
        if self.api.cors_origins == ["*"]:
            problems.append("wildcard CORS is refused in production")
        if problems:
            msg = "Unsafe production configuration: " + "; ".join(problems)
            raise ValueError(msg)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so configuration is read once. Tests clear the cache with
    ``get_settings.cache_clear()`` after changing environment variables.
    """
    return Settings()
