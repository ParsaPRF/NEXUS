"""Security primitives: password hashing, credential encryption, API-key authentication.

Three separate concerns live here because they are all "cryptography the application needs",
and keeping them together makes them easy to audit:

1. **Passwords** - one-way hashing (Argon2id). We must never be able to recover a password,
   only verify one.
2. **Proxy credentials** - *reversible* encryption (Fernet/AES). A proxy password has to be
   replayed to the proxy, so hashing is impossible; encryption plus a key held only in the
   environment is the correct tool. If no key is configured we refuse to store the credential
   rather than keep it in plain text (docs/SECURITY.md T5).
3. **API authentication** - a shared API key compared in constant time, resolved to a role so
   authorization can be added without restructuring the dependency.

A note on constant-time comparison: comparing secrets with ``==`` leaks their length and
content through timing. ``hmac.compare_digest`` does not.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Header, Request, status

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    CredentialsNotStorableError,
    ValidationError,
)

# Argon2id with library defaults: memory-hard, so GPU cracking is expensive.
# ``PasswordHasher`` is safe to share across threads.
_hasher = PasswordHasher()


class Role(StrEnum):
    """Application roles. ``ADMIN`` may change state; ``VIEWER`` may only read."""

    ADMIN = "admin"
    VIEWER = "viewer"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller.

    Deliberately tiny: the API key maps to one configured principal in this local-first
    product. When real user accounts are added, only :func:`get_principal` changes.
    """

    name: str
    role: Role

    @property
    def is_admin(self) -> bool:
        """True when the principal may perform state-changing operations."""
        return self.role is Role.ADMIN


# --------------------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with Argon2id.

    Raises:
        ValidationError: the password is too short to be worth hashing.
    """
    if len(password) < 8:
        raise ValidationError(
            "Password must be at least 8 characters.", details={"field": "password"}
        )
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return ``True`` when ``password`` matches ``password_hash``. Never raises.

    Every failure mode has to be caught here, because a corrupt or truncated hash in the
    database must produce "login refused", never an unhandled exception and a 500. ``argon2``
    signals all of them through ``VerificationError`` (of which ``VerifyMismatchError`` is a
    subclass) and a malformed input can additionally surface as ``ValueError`` or ``TypeError``.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated Argon2 parameters and should be upgraded."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        return True


def generate_secret(length: int = 32) -> str:
    """Generate a URL-safe random secret (API keys, synthetic passwords, inbox aliases)."""
    if length < 8:
        raise ValidationError("Secret length must be at least 8.", details={"length": length})
    return secrets.token_urlsafe(length)


# --------------------------------------------------------------------------------------
# Reversible credential encryption (proxy passwords only)
# --------------------------------------------------------------------------------------


def _derive_fernet_key(raw_key: str) -> bytes:
    """Turn an arbitrary configured string into a valid 32-byte Fernet key.

    Operators should not have to know that Fernet wants 32 url-safe base64 bytes, so we accept
    any sufficiently long passphrase and derive the key with SHA-256. If they *do* supply a
    proper Fernet key we use it unchanged.
    """
    candidate = raw_key.strip().encode()
    try:
        decoded = base64.urlsafe_b64decode(candidate)
        if len(decoded) == 32:
            return candidate
    except (ValueError, TypeError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())


class CredentialCipher:
    """Encrypts and decrypts proxy credentials using a key from the environment."""

    def __init__(self, raw_key: str) -> None:
        if not raw_key:
            raise CredentialsNotStorableError()
        self._fernet = Fernet(_derive_fernet_key(raw_key))

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` for storage."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a stored value.

        Raises:
            CredentialsNotStorableError: the ciphertext cannot be decrypted, which normally
                means ``NEXUS_PROXY_CREDENTIAL_KEY`` changed. We fail loudly instead of
                silently using a wrong password.
        """
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise CredentialsNotStorableError(
                "Stored proxy credentials could not be decrypted. "
                "The credential key has probably changed.",
                details={"reason": "invalid_token"},
            ) from exc


def get_cipher(settings: Annotated[Settings, Depends(get_settings)]) -> CredentialCipher:
    """FastAPI dependency returning a credential cipher built from settings."""
    return CredentialCipher(settings.proxy_credential_key)


# --------------------------------------------------------------------------------------
# API authentication
# --------------------------------------------------------------------------------------


def _constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking their contents through timing."""
    return hmac.compare_digest(left.encode(), right.encode())


async def get_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Authenticate the request from its ``X-API-Key`` header.

    Behaviour when ``NEXUS_API_KEY`` is empty (development only): an implicit admin principal
    is returned *and a warning is logged*, because an unauthenticated API is a real risk and
    silently allowing it would be worse than saying so.

    Raises:
        AuthenticationError: the key is missing or wrong.
    """
    if not settings.api_key:
        if settings.is_production:  # pragma: no cover - refused at startup as well
            raise AuthenticationError("API authentication must be configured in production.")
        from app.core.logging import get_logger

        get_logger(__name__).warning(
            "NEXUS_API_KEY is not configured - the API is running WITHOUT authentication. "
            "This is only acceptable for local development."
        )
        return Principal(name="anonymous-local", role=Role.ADMIN)

    if not x_api_key:
        raise AuthenticationError("The X-API-Key header is required.")

    admin_key = settings.api_key
    # Optional read-only key so a monitoring tool can be granted GET access only.
    viewer_key = getattr(settings, "viewer_api_key", "") or ""

    if _constant_time_equals(x_api_key, admin_key):
        principal = Principal(name="admin", role=Role.ADMIN)
    elif viewer_key and _constant_time_equals(x_api_key, viewer_key):
        principal = Principal(name="viewer", role=Role.VIEWER)
    else:
        raise AuthenticationError("The supplied API key is not valid.")

    # Make the caller visible to the audit layer and the logs.
    request.state.principal = principal
    return principal


async def require_admin(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Dependency that additionally requires the admin role.

    Raises:
        AuthorizationError: the caller is authenticated as a read-only principal.
    """
    if not principal.is_admin:
        raise AuthorizationError("This operation requires the admin role.")
    return principal


# Re-exported for route decorators so status codes are written once.
UNAUTHORIZED = status.HTTP_401_UNAUTHORIZED
FORBIDDEN = status.HTTP_403_FORBIDDEN
