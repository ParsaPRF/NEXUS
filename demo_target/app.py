"""Bundled demonstration registration application.

This is a **complete, self-contained** web application that NEXUS may test against. It exists so
the Registration Lab can be exercised end to end without touching anyone else's system: the
correct default target is also the easy one.

It is deliberately a *separate* application, not a module inside NEXUS's backend:

* it has its own code and its own state, like a real target would;
* NEXUS talks to it over HTTP, exactly as it would to an authorized staging environment;
* nothing in it imports ``app.*``, so it cannot accidentally share NEXUS's database or secrets.

Registration flow::

    POST /api/register  -> 202 pending_verification, emails a 6-digit code
    POST /api/verify    -> 200 active (or 400 wrong/expired code)
    GET  /api/status/…  -> missing | pending_verification | active

Safety posture: it can only send mail through the mailer it is constructed with. The default
mailer talks to Mailpit on loopback, which cannot deliver to the internet. It also exposes a
simulation switch (``POST /api/_simulate``) so the "stop on anti-abuse control" behaviour can be
tested honestly rather than being untestable.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

#: How long a verification code stays valid.
CODE_TTL = timedelta(minutes=10)

#: How many wrong attempts before the code is invalidated.
MAX_CODE_ATTEMPTS = 5

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------------------
# Mail delivery
# --------------------------------------------------------------------------------------


class Mailer(Protocol):
    """Anything that can deliver a plain-text message."""

    async def send(self, *, to: str, subject: str, text: str) -> None: ...


class CallbackMailer:
    """Mailer that hands the message to a callback.

    Used by tests to deliver straight into NEXUS's in-memory mail backend, so the whole
    register → email → verify loop runs with no SMTP and no network.
    """

    def __init__(self, callback) -> None:  # type: ignore[no-untyped-def]
        self._callback = callback

    async def send(self, *, to: str, subject: str, text: str) -> None:
        self._callback(to, subject, text)


class SmtpMailer:
    """Mailer that submits to an SMTP server (Mailpit in the compose stack)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1025, sender: str = "no-reply@demo.test") -> None:
        self._host = host
        self._port = port
        self._sender = sender

    async def send(self, *, to: str, subject: str, text: str) -> None:
        # smtplib is blocking; running it in a thread keeps the event loop free.
        await asyncio.to_thread(self._send_sync, to, subject, text)

    def _send_sync(self, to: str, subject: str, text: str) -> None:
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        with smtplib.SMTP(self._host, self._port, timeout=5) as client:
            client.send_message(message)


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-SHA256 (independent of NEXUS's Argon2 setup).

    A real target application would use Argon2 or bcrypt; PBKDF2 with a per-user random salt is
    the standard-library equivalent and keeps this demo free of extra dependencies.
    """
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return f"pbkdf2_sha256$100000${salt.hex()}${derived.hex()}"


def verify_password_hash(password: str, stored: str) -> bool:
    """Check a password against a PBKDF2 hash, in constant time."""
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


@dataclass
class DemoUser:
    """One account in the demonstration application."""

    email: str
    password_hash: str
    code: str
    code_expires_at: datetime
    status: str = "pending_verification"
    attempts: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class DemoStore:
    """In-memory account store.

    In-memory on purpose: the demonstration application is disposable, so it needs no migrations
    and leaves nothing behind. Tests get a clean store per application instance simply by
    constructing a new one.
    """

    def __init__(self) -> None:
        self._users: dict[str, DemoUser] = {}
        self._lock = asyncio.Lock()

    async def create(self, email: str, password_hash: str, code: str) -> DemoUser:
        """Create a pending account, or raise if the address is taken."""
        async with self._lock:
            key = email.lower()
            if key in self._users:
                raise ValueError("exists")
            user = DemoUser(
                email=key,
                password_hash=password_hash,
                code=code,
                code_expires_at=datetime.now(UTC) + CODE_TTL,
            )
            self._users[key] = user
            return user

    async def get(self, email: str) -> DemoUser | None:
        """Return an account by address."""
        return self._users.get(email.lower())

    async def verify(self, email: str, code: str) -> str:
        """Verify a code and activate the account.

        Returns the resulting status: ``active``, ``invalid``, ``expired`` or ``missing``.
        """
        async with self._lock:
            user = self._users.get(email.lower())
            if user is None:
                return "missing"
            if user.status == "active":
                return "active"
            if datetime.now(UTC) > user.code_expires_at:
                return "expired"
            if user.attempts >= MAX_CODE_ATTEMPTS:
                return "expired"
            user.attempts += 1
            if not secrets.compare_digest(user.code, code):
                return "invalid"
            user.status = "active"
            return "active"

    def all_users(self) -> list[DemoUser]:
        """Return every account (used by tests)."""
        return list(self._users.values())


# --------------------------------------------------------------------------------------
# Request/response models
# --------------------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Payload for ``POST /api/register``."""

    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            msg = "Enter a valid email address"
            raise ValueError(msg)
        return value.lower()


class VerifyRequest(BaseModel):
    """Payload for ``POST /api/verify``."""

    email: str = Field(min_length=5, max_length=320)
    code: str = Field(min_length=4, max_length=8)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            msg = "Enter a valid email address"
            raise ValueError(msg)
        return value.lower()


class SimulationRequest(BaseModel):
    """Payload for ``POST /api/_simulate``."""

    mode: str = Field(
        default="none",
        description="none | rate_limit | captcha | access_denied | slow",
    )


# --------------------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------------------


def create_demo_app(*, mailer: Mailer | None = None) -> FastAPI:
    """Build the demonstration application.

    Args:
        mailer: How to deliver verification emails. Defaults to SMTP to Mailpit on loopback.
    """
    app = FastAPI(
        title="NEXUS demo target",
        description=(
            "Demonstration registration application used as NEXUS's default test target. "
            "Owned by this repository; not a third-party service."
        ),
        version="0.1.0",
    )
    app.state.store = DemoStore()
    app.state.mailer = mailer or SmtpMailer(
        host=os.environ.get("DEMO_TARGET_SMTP_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEMO_TARGET_SMTP_PORT", "1025")),
    )
    app.state.simulation = {"mode": "none"}

    # ---------------------------------------------------------------- simulation

    @app.post("/api/_simulate", include_in_schema=False)
    async def simulate(payload: SimulationRequest, request: Request) -> dict[str, str]:
        """Force the target to answer with an anti-abuse control.

        Demo-only switch. NEXUS must *stop* when it meets one of these, and an untestable
        requirement is not a requirement.
        """
        request.app.state.simulation["mode"] = payload.mode
        return {"mode": payload.mode}

    # ---------------------------------------------------------------- registration

    @app.post("/api/register", status_code=status.HTTP_202_ACCEPTED)
    async def register(payload: RegisterRequest, request: Request) -> dict[str, str]:
        """Start a registration: store the account pending and email a code."""
        mode = request.app.state.simulation["mode"]
        if mode == "rate_limit":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many registration attempts. Try again later.",
            )
        if mode == "captcha":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="CAPTCHA verification required before continuing.",
            )
        if mode == "access_denied":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration is not available for this client.",
            )
        if mode == "slow":
            await asyncio.sleep(5)

        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            await request.app.state.store.create(
                payload.email, hash_password(payload.password), code
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            ) from exc

        await request.app.state.mailer.send(
            to=payload.email,
            subject="Your verification code",
            text=(
                f"Welcome. Your verification code is {code}.\n"
                "It expires in 10 minutes.\n"
            ),
        )
        return {"status": "pending_verification"}

    @app.post("/api/verify")
    async def verify(payload: VerifyRequest, request: Request) -> dict[str, str]:
        """Complete registration by submitting the emailed code."""
        outcome = await request.app.state.store.verify(payload.email, payload.code)
        if outcome == "active":
            return {"status": "active"}
        if outcome == "missing":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown account.")
        if outcome == "expired":
            raise HTTPException(
                status_code=status.HTTP_410_GONE, detail="The verification code has expired."
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That verification code is not valid."
        )

    @app.get("/api/status/{email}")
    async def account_status(email: str, request: Request) -> dict[str, Any]:
        """Report an account's state, which is how a test asserts success."""
        user = await request.app.state.store.get(email)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown account.")
        return {"email": user.email, "status": user.status}

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness for the demonstration application."""
        return {"status": "ok"}

    return app


#: Module-level application for ``uvicorn demo_target.app:app``.
app = create_demo_app()