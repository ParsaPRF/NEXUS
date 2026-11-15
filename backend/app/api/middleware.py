"""ASGI middleware: request correlation, access logging and disaster containment.

Three small pieces, each solving one problem:

* :class:`RequestContextMiddleware` gives every request an id and makes it visible to logs, the
  error body and the audit trail. Without it, debugging a failure means guessing which of the
  interleaved log lines belong together.
* :class:`AccessLogMiddleware` writes one structured line per request with method, path, status
  and duration. It logs the *path only* - never the query string, which can carry tokens, and
  never the body.
* :class:`SecurityHeadersMiddleware` sets conservative response headers. They are cheap, and
  they matter because the OpenAPI docs page is served by the same application.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger, set_request_id

logger = get_logger("nexus.access")

REQUEST_ID_HEADER = "X-Request-ID"

#: Headers applied to every response.
#:
#: ``X-Content-Type-Options: nosniff`` prevents a browser from re-interpreting a JSON response
#: as HTML or script. ``Referrer-Policy`` keeps the local URL out of any outbound link. No
#: ``Access-Control-Allow-Origin`` is set here: CORS is opt-in through configuration, because a
#: wildcard would let any web page the user visits talk to their local NEXUS instance.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id and expose it on ``request.state`` and the response headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Honour an inbound id so a request can be traced across services, but bound its length
        # and strip anything odd: it ends up in logs and response headers.
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _is_plausible_request_id(incoming) else uuid.uuid4().hex[:16]
        set_request_id(request_id)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log one line per request: method, path, status, duration, request id."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            # Path only: a query string may contain a token, and the body may contain anything.
            logger.exception(
                "%s %s failed after %.2fms",
                request.method,
                request.url.path,
                duration_ms,
                extra={"module_name": "access"},
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        # Health probes are polled constantly; logging them at INFO would drown everything else.
        level = logger.debug if request.url.path.startswith("/health") else logger.info
        level(
            "%s %s -> %s in %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={"module_name": "access"},
        )
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.2f}"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply :data:`SECURITY_HEADERS` to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def _is_plausible_request_id(value: str) -> bool:
    """Accept only short, printable, token-like request ids from the client."""
    if not value or len(value) > 64:
        return False
    return all(char.isalnum() or char in "-_." for char in value)


__all__ = [
    "AccessLogMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "SECURITY_HEADERS",
    "REQUEST_ID_HEADER",
]
