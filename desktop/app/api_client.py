"""HTTP client for the NEXUS API.

This module has **no Qt imports** on purpose. All the logic that can be wrong - URL building,
authentication, error translation, response parsing - lives here and is tested with an injected
transport, in milliseconds, without a Qt event loop. The views stay thin enough to be obviously
correct.

The client is synchronous and is meant to be called from a worker thread (see ``app.tasks``).
Using an async client would force an event loop into a Qt application, which is the one thing the
architecture forbids (docs/ARCHITECTURE.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import DesktopConfig

Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ApiError:
    """A failure the API reported, or a transport failure.

    ``code`` is the backend's stable machine-readable string; the UI branches on it rather than on
    the message text, which may be reworded.
    """

    code: str
    message: str
    status: int | None = None
    request_id: str | None = None
    details: Json | None = None

    @property
    def is_auth_error(self) -> bool:
        """True when the API key is missing or wrong."""
        return self.status == 401 or self.code in {"unauthenticated", "forbidden"}  # noqa: PLR2004

    @property
    def is_connection_error(self) -> bool:
        """True when the backend could not be reached at all."""
        return self.status is None

    def __str__(self) -> str:
        suffix = f" (request {self.request_id})" if self.request_id else ""
        return f"{self.message}{suffix}"


class NexusApiError(Exception):
    """Raised for every API call that does not succeed, carrying an :class:`ApiError`."""

    def __init__(self, error: ApiError) -> None:
        self.error = error
        super().__init__(str(error))


class NexusClient:
    """A thin, typed wrapper over the NEXUS REST API."""

    def __init__(
        self,
        config: DesktopConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        headers = {"Accept": "application/json"}
        if config.api_key:
            headers["X-API-Key"] = config.api_key
        self._client = httpx.Client(
            base_url=config.base_url,
            headers=headers,
            timeout=httpx.Timeout(config.timeout_seconds),
            verify=config.verify_tls,
            # An environment proxy would silently reroute the client's traffic; a desktop tool
            # talking to loopback should never do that.
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> NexusClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform a request, translating every failure into :class:`NexusApiError`."""
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise NexusApiError(
                ApiError(code="timeout", message=f"The API did not answer in time: {exc}")
            ) from exc
        except httpx.HTTPError as exc:
            raise NexusApiError(
                ApiError(
                    code="connection_error",
                    message=(
                        f"Could not reach the NEXUS API at {self._config.base_url}. "
                        "Is the backend running?"
                    ),
                    details={"reason": str(exc)[:200]},
                )
            ) from exc

        if response.status_code >= 400:  # noqa: PLR2004
            raise NexusApiError(_error_from_response(response))

        if response.status_code == 204 or not response.content:  # noqa: PLR2004
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    def _get(self, path: str, **params: Any) -> Any:
        clean = {key: value for key, value in params.items() if value not in (None, "", [])}
        return self._request("GET", path, params=clean)

    # ------------------------------------------------------------------ instance

    def health(self) -> Json:
        """Liveness probe (unauthenticated)."""
        return self._request("GET", "/health")

    def readiness(self) -> Json:
        """Readiness probe with per-dependency state (unauthenticated)."""
        return self._request("GET", "/health/ready")

    def instance_info(self) -> Json:
        """Describe the backend instance."""
        return self._request("GET", "/api/v1/meta")

    def safety_limits(self) -> Json:
        """The safety limits currently in force."""
        return self._request("GET", "/api/v1/meta/limits")

    # ------------------------------------------------------------------ proxies

    def list_proxies(self, **params: Any) -> Json:
        """A page of proxies."""
        return self._get("/api/v1/proxies", **params)

    def proxy_stats(self) -> Json:
        """Proxy counts by status."""
        return self._request("GET", "/api/v1/proxies/stats")

    def import_proxies(
        self, content: str, *, fmt: str = "auto", name: str = "Desktop import"
    ) -> Json:
        """Import a proxy list."""
        return self._request(
            "POST",
            "/api/v1/proxy-sources",
            json={"content": content, "format": fmt, "name": name, "kind": "manual"},
        )

    def check_proxy(self, proxy_id: int) -> Json:
        """Health-check one proxy."""
        return self._request("POST", f"/api/v1/proxies/{proxy_id}/check")

    def check_proxies(self, proxy_ids: list[int]) -> Json:
        """Queue a batch health check."""
        return self._request("POST", "/api/v1/proxies/check-batch", json={"proxy_ids": proxy_ids})

    def set_proxy_enabled(self, proxy_id: int, *, enabled: bool) -> Json:
        """Enable or disable a proxy."""
        return self._request("PATCH", f"/api/v1/proxies/{proxy_id}", json={"is_enabled": enabled})

    def delete_proxy(self, proxy_id: int) -> None:
        """Delete a proxy and its history."""
        self._request("DELETE", f"/api/v1/proxies/{proxy_id}")

    def export_proxies(self, *, fmt: str = "csv", **params: Any) -> str:
        """Export proxies as CSV or JSON text."""
        return self._get("/api/v1/proxies/export", format=fmt, **params)

    # ------------------------------------------------------------------ inboxes

    def list_inboxes(self, **params: Any) -> Json:
        """A page of inboxes."""
        return self._get("/api/v1/inboxes", **params)

    def list_domains(self) -> list[Json]:
        """Configured inbox domains."""
        return self._request("GET", "/api/v1/email-domains")

    def create_inbox(self, **payload: Any) -> Json:
        """Create one test inbox."""
        return self._request("POST", "/api/v1/inboxes", json=payload)

    def bulk_create_inboxes(self, count: int, *, label_prefix: str | None = None) -> Json:
        """Create several test inboxes."""
        return self._request(
            "POST",
            "/api/v1/inboxes/bulk",
            json={"count": count, "label_prefix": label_prefix},
        )

    def poll_inbox(self, public_id: str) -> Json:
        """Ingest new messages for one inbox."""
        return self._request("POST", f"/api/v1/inboxes/{public_id}/poll")

    def delete_inbox(self, public_id: str) -> None:
        """Delete an inbox and its messages."""
        self._request("DELETE", f"/api/v1/inboxes/{public_id}")

    def cleanup_inboxes(self) -> Json:
        """Expire overdue inboxes."""
        return self._request("POST", "/api/v1/inboxes/cleanup")

    def list_messages(self, public_id: str, **params: Any) -> Json:
        """A page of an inbox's messages."""
        return self._get(f"/api/v1/inboxes/{public_id}/messages", **params)

    def get_message(self, message_id: int) -> Json:
        """One message, including bodies."""
        return self._request("GET", f"/api/v1/messages/{message_id}")

    def extract_code(self, message_id: int) -> Json:
        """Re-run verification-code extraction for a message."""
        return self._request("POST", f"/api/v1/messages/{message_id}/extract-code")

    def export_messages(self, public_id: str, *, fmt: str = "csv") -> str:
        """Export an inbox's messages."""
        return self._get(f"/api/v1/inboxes/{public_id}/export", format=fmt)

    # ------------------------------------------------------------------ registration

    def list_targets(self) -> list[Json]:
        """Registered registration targets."""
        return self._request("GET", "/api/v1/targets")

    def create_target(self, **payload: Any) -> Json:
        """Register a target."""
        return self._request("POST", "/api/v1/targets", json=payload)

    def authorize_target(self, target_id: int, note: str) -> Json:
        """Record that a target is authorized."""
        return self._request(
            "POST", f"/api/v1/targets/{target_id}/authorize", params={"note": note}
        )

    def list_campaigns(self, **params: Any) -> Json:
        """A page of campaigns."""
        return self._get("/api/v1/campaigns", **params)

    def create_campaign(self, **payload: Any) -> Json:
        """Create and queue a campaign."""
        return self._request("POST", "/api/v1/campaigns", json=payload)

    def campaign_results(self, public_id: str, **params: Any) -> Json:
        """Per-test results for a campaign."""
        return self._get(f"/api/v1/campaigns/{public_id}/results", **params)

    def cancel_campaign(self, public_id: str) -> Json:
        """Cancel a campaign."""
        return self._request("POST", f"/api/v1/campaigns/{public_id}/cancel")

    # ------------------------------------------------------------------ jobs & analytics

    def list_jobs(self, **params: Any) -> Json:
        """A page of background jobs."""
        return self._get("/api/v1/jobs", **params)

    def get_job(self, public_id: str) -> Json:
        """One job with its logs."""
        return self._request("GET", f"/api/v1/jobs/{public_id}")

    def cancel_job(self, public_id: str) -> Json:
        """Cancel a job."""
        return self._request("POST", f"/api/v1/jobs/{public_id}/cancel")

    def analytics_summary(self) -> Json:
        """Counts across every module."""
        return self._request("GET", "/api/v1/analytics/summary")

    def export_analytics(self, *, dataset: str = "jobs", fmt: str = "csv") -> str:
        """Export jobs or campaigns."""
        return self._get("/api/v1/analytics/export", dataset=dataset, format=fmt)


def _error_from_response(response: httpx.Response) -> ApiError:
    """Translate an error response into an :class:`ApiError`.

    The backend always answers with the same shape, but a proxy or a crashed process might not -
    so a non-JSON body degrades to a readable message instead of raising a parsing error.
    """
    try:
        payload = response.json()
    except ValueError:
        return ApiError(
            code=f"http_{response.status_code}",
            message=f"The API returned HTTP {response.status_code}.",
            status=response.status_code,
        )
    return ApiError(
        code=str(payload.get("code", f"http_{response.status_code}")),
        message=str(payload.get("message", "The request failed.")),
        status=response.status_code,
        request_id=payload.get("request_id"),
        details=payload.get("details"),
    )
