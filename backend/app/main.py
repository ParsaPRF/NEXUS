"""NEXUS FastAPI application entry point.

Assembly happens in :func:`create_app` rather than at module level so tests can build an
application with different settings. ``app.main:app`` still exists for uvicorn, because a
factory-only module would make the standard ``uvicorn app.main:app`` command fail.

Startup sequence (the order matters):

1. Validate settings, including the production safety check that refuses to boot with
   development-only shortcuts enabled.
2. Configure logging with redaction.
3. Import the model package so every table is registered in ``Base.metadata`` *before* any
   database work - a model that has not been imported is invisible to Alembic (see
   :mod:`app.models`).
4. Create the async engine and session factory.
5. Initialise the job queue backend.

Shutdown reverses steps 4 and 5 so connections are closed cleanly.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.middleware import (
    AccessLogMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.router import api_router, operational_router
from app.core.config import Settings, get_settings
from app.core.exceptions import NexusError
from app.core.logging import configure_logging, get_logger
from app.database.engine import dispose_engine, init_engine
from app.jobs.queue import get_queue, reset_queue
from app.schemas.common import ErrorResponse

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the resources the application needs.

    Using the lifespan context (instead of the deprecated ``@app.on_event``) means startup and
    shutdown live in one place and are guaranteed to be paired.
    """
    settings: Settings = app.state.settings

    # 1. Fail fast on an unsafe production configuration, before opening anything.
    settings.enforce_production_safety()

    # 2. Logging first, so that every later step is observable and redacted.
    configure_logging(
        level=settings.log_level,
        log_format=settings.log_format,
        log_directory=settings.log_directory if settings.is_production else None,
    )

    logger.info(
        "Starting %s %s (profile=%s, environment=%s)",
        settings.app_name,
        __version__,
        settings.profile.value,
        settings.environment,
        extra={"module_name": "startup"},
    )

    # 3. Import the model package so all mappers and tables are registered.
    import app.models  # noqa: F401  (import for side effect: registers every table)

    # 4. Database.
    init_engine(settings)
    logger.info(
        "Database engine ready (dialect=%s)",
        settings.database.url.split(":", 1)[0],
        extra={"module_name": "startup"},
    )

    # 5. Job queue.
    queue = get_queue(settings)
    logger.info(
        "Job queue ready (backend=%s)",
        type(queue).__name__,
        extra={"module_name": "startup"},
    )

    if settings.api.cors_origins:
        logger.info(
            "CORS enabled for: %s",
            ", ".join(settings.api.cors_origins),
            extra={"module_name": "startup"},
        )
    if not settings.api_key:
        logger.warning(
            "NEXUS_API_KEY is not set: the API is UNAUTHENTICATED. This is acceptable only for "
            "local development.",
            extra={"module_name": "startup"},
        )

    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.app_name, extra={"module_name": "shutdown"})
        await reset_queue()
        await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Override settings, used by tests. When omitted, the process-wide settings
            singleton is used.

    Returns:
        A configured, ready-to-serve application (the lifespan still runs on startup).
    """
    resolved = settings or get_settings()

    app = FastAPI(
        title=f"{resolved.app_name} API",
        description=(
            "Local-first platform for authorized network resource management, disposable test "
            "inboxes, and registration testing in owned or explicitly authorized applications.\n\n"
            "**Scope:** NEXUS only operates against systems the user owns or is explicitly "
            "authorized to test. It does not create third-party mailbox accounts and does not "
            "bypass anti-abuse controls."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if resolved.api.docs_enabled else None,
        redoc_url="/redoc" if resolved.api.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.api.docs_enabled else None,
        root_path=resolved.api.root_path,
    )
    # Routes and the lifespan read settings from here, so a test-built app is fully isolated
    # from the process-wide singleton.
    app.state.settings = resolved

    # Routes declare ``Depends(get_settings)``, which would otherwise return the *process-wide*
    # cached settings and ignore the object passed to create_app. Overriding the dependency is
    # what makes per-test isolation real rather than cosmetic (and it is also the supported way
    # to swap configuration in production if it is ever loaded at runtime).
    from app.core.config import get_settings as _get_settings

    app.dependency_overrides[_get_settings] = lambda: resolved

    _register_middleware(app, resolved)
    _register_exception_handlers(app)

    app.include_router(operational_router)
    app.include_router(api_router)

    return app


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    """Attach middleware.

    Ordering note: Starlette applies middleware in reverse registration order, so the *last*
    added runs first on the way in. We want ``RequestContextMiddleware`` to run outermost -
    it assigns the request id that the access log line reports - so it is added last.
    """
    if settings.api.cors_origins:
        # Opt-in only, and never a wildcard: a permissive CORS policy would let any web page
        # the user happens to visit drive their local NEXUS instance from their browser.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["X-API-Key", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-Response-Time-ms"],
        )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestContextMiddleware)


def _register_exception_handlers(app: FastAPI) -> None:
    """Map exception types to HTTP responses in exactly one place.

    This is the counterpart to the rule that services raise domain errors instead of
    ``HTTPException``: every translation between the domain and HTTP happens here.
    """

    @app.exception_handler(NexusError)
    async def handle_nexus_error(request: Request, exc: NexusError) -> JSONResponse:
        """Domain error -> its declared status code. No 500s, no leaking internals."""
        request_id = getattr(request.state, "request_id", None)
        # Denials and errors are worth knowing about; an expected 404 is not.
        log = logger.warning if exc.http_status >= 400 else logger.info
        log(
            "Domain error: %s (%s) on %s %s",
            exc.code,
            exc.message,
            request.method,
            request.url.path,
            extra={"module_name": "errors", "error_code": exc.code},
        )
        body = exc.to_dict()
        body["request_id"] = request_id
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Pydantic rejected the request body or query parameters -> 422 with field detail."""
        body = ErrorResponse(
            code="request_validation_error",
            message="The request payload or parameters are invalid.",
            # ``errors()`` includes the input value, which may be a password. Keep the location
            # and the reason; drop the raw input.
            details={
                "errors": [
                    {
                        "location": list(error.get("loc", ())),
                        "message": error.get("msg", ""),
                        "type": error.get("type", ""),
                    }
                    for error in exc.errors()
                ]
            },
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=422, content=body.model_dump(exclude_none=True))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Framework-raised HTTP errors (404 for an unknown route, 405, and so on)."""
        body = ErrorResponse(
            code=_code_for_status(exc.status_code),
            message=str(exc.detail),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(exclude_none=True),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        """Database failures -> 503 with a generic message.

        The exception text can contain SQL, table names and even parameter values, so it is
        logged in full but never returned to the caller.
        """
        logger.exception(
            "Database error on %s %s",
            request.method,
            request.url.path,
            extra={"module_name": "errors"},
        )
        body = ErrorResponse(
            code="database_error",
            message="A database error occurred. See the server logs for the request id.",
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=503, content=body.model_dump(exclude_none=True))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: 500 with a request id and nothing else.

        Tracebacks and messages routinely contain file paths, SQL and configuration values, so
        the response carries only the id the operator needs to find the full story in the logs.
        """
        logger.exception(
            "Unhandled %s on %s %s",
            type(exc).__name__,
            request.method,
            request.url.path,
            extra={"module_name": "errors"},
        )
        body = ErrorResponse(
            code="internal_error",
            message="An unexpected error occurred. Quote the request id when reporting it.",
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=500, content=body.model_dump(exclude_none=True))


def _code_for_status(status_code: int) -> str:
    """Return a stable error code for a framework HTTP status."""
    return {
        400: "bad_request",
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "request_validation_error",
        429: "rate_limited",
        500: "internal_error",
        503: "service_unavailable",
    }.get(status_code, f"http_{status_code}")


# Module-level application for ``uvicorn app.main:app``.
app = create_app()


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.api.host,
        port=_settings.api.port,
        reload=True,
        log_level=logging.getLevelName(_settings.log_level).lower(),
    )
