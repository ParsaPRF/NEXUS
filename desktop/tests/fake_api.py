"""A fake NEXUS API for desktop tests.

The client is exercised against an ``httpx.MockTransport`` serving the *same shapes the real
backend returns* (copied from docs/API_REFERENCE.md). That means the tests fail if the client
starts expecting a field the API does not send - which is exactly the drift a desktop client is
prone to and a live-server test would only catch sometimes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

Json = dict[str, Any]


def proxy_row(proxy_id: int = 1, **overrides: Any) -> Json:
    """One proxy as the API returns it."""
    row: Json = {
        "id": proxy_id,
        "source_id": 1,
        "protocol": "http",
        "host": f"proxy{proxy_id}.example.test",
        "port": 8080,
        "username": None,
        "status": "healthy",
        "latency_ms": 37,
        "reliability": 1.0,
        "success_count": 1,
        "failure_count": 0,
        "consecutive_failures": 0,
        "last_checked_at": "2026-11-16T04:00:00Z",
        "last_success_at": "2026-11-16T04:00:00Z",
        "last_error": None,
        "is_enabled": True,
        "created_at": "2026-11-16T03:59:00Z",
    }
    row.update(overrides)
    return row


def inbox_row(public_id: str = "inbox-1", **overrides: Any) -> Json:
    """One inbox as the API returns it."""
    row: Json = {
        "public_id": public_id,
        "address": f"qa-{public_id}@example.test",
        "local_part": f"qa-{public_id}",
        "domain": "example.test",
        "label": "desktop",
        "status": "active",
        "message_count": 1,
        "last_message_at": "2026-11-16T04:01:00Z",
        "expires_at": "2026-11-17T04:00:00Z",
        "campaign_id": None,
        "created_at": "2026-11-16T03:58:00Z",
    }
    row.update(overrides)
    return row


def message_row(message_id: int = 1, **overrides: Any) -> Json:
    """One message summary as the API returns it."""
    row: Json = {
        "id": message_id,
        "inbox_id": 1,
        "sender": "no-reply@demo.test",
        "recipient": "qa-inbox-1@example.test",
        "subject": "Your verification code",
        "received_at": "2026-11-16T04:01:00Z",
        "size_bytes": 512,
        "extracted_code": "481920",
        "extraction_confidence": 0.9,
    }
    row.update(overrides)
    return row


def target_row(target_id: int = 1, **overrides: Any) -> Json:
    """One registration target."""
    row: Json = {
        "id": target_id,
        "name": "Bundled demo application",
        "base_url": "http://127.0.0.1:9000",
        "kind": "demo",
        "adapter_name": "demo_api",
        "is_authorized": True,
        "authorization_note": "Owned by this repository.",
        "is_active": True,
        "may_be_tested": True,
        "created_at": "2026-11-16T03:50:00Z",
    }
    row.update(overrides)
    return row


def campaign_row(public_id: str = "campaign-1", **overrides: Any) -> Json:
    """One campaign."""
    row: Json = {
        "public_id": public_id,
        "name": "Desktop campaign",
        "target_id": 1,
        "status": "completed",
        "total_tests": 3,
        "completed_tests": 3,
        "succeeded_tests": 3,
        "failed_tests": 0,
        "progress_percent": 100.0,
        "concurrency": 1,
        "rate_limit_per_minute": 30,
        "started_at": "2026-11-16T04:00:00Z",
        "finished_at": "2026-11-16T04:00:05Z",
        "stop_reason": None,
        "created_at": "2026-11-16T03:59:00Z",
    }
    row.update(overrides)
    return row


def job_row(public_id: str = "job-1", **overrides: Any) -> Json:
    """One background job."""
    row: Json = {
        "public_id": public_id,
        "job_type": "proxy.check_batch",
        "status": "completed",
        "progress_current": 3,
        "progress_total": 3,
        "progress_percent": 100.0,
        "attempts": 1,
        "duration_ms": 412,
        "queued_at": "2026-11-16T04:00:00Z",
        "started_at": "2026-11-16T04:00:00Z",
        "finished_at": "2026-11-16T04:00:01Z",
        "error_message": None,
        "cancel_requested": False,
        "idempotency_key": None,
    }
    row.update(overrides)
    return row


def default_routes() -> dict[tuple[str, str], Json]:
    """The default response for every endpoint the client uses."""
    return {
        ("GET", "/health"): {"status": "ok", "app": "NEXUS", "profile": "local"},
        ("GET", "/health/ready"): {
            "ready": True,
            "checks": [
                {"name": "database", "state": "ok", "detail": "2ms"},
                {"name": "security", "state": "ok", "detail": None},
            ],
        },
        ("GET", "/api/v1/meta"): {
            "app": "NEXUS",
            "version": "0.1.0",
            "environment": "development",
            "profile": "local",
            "auth_enabled": True,
            "database_dialect": "sqlite",
            "mail_backend": "memory",
            "job_backend": "inprocess",
        },
        ("GET", "/api/v1/meta/limits"): {
            "allow_private_targets": False,
            "allowed_target_hosts": [],
            "http_timeout_seconds": 15.0,
            "max_concurrent_checks": 10,
            "max_response_bytes": 1048576,
            "registration_disabled_reason": "No authorized target hosts are configured.",
        },
        ("GET", "/api/v1/analytics/summary"): {
            "jobs": {"completed": 4, "running": 1},
            "proxies": {"healthy": 2, "expired": 1},
            "inboxes": {"active": 3},
            "campaigns": {"completed": 1, "running": 1},
            "total_jobs": 5,
            "total_proxies": 3,
            "total_inboxes": 3,
            "total_campaigns": 2,
            "active_inboxes": 3,
            "healthy_proxies": 2,
            "running_campaigns": 1,
            "running_jobs": 1,
            "completed_jobs": 4,
            "failed_jobs": 1,
            "recent_activity": [
                {
                    "action": "proxy.import",
                    "entity_type": "proxy_source",
                    "entity_id": "1",
                    "outcome": "success",
                    "created_at": "2026-11-16T04:00:00Z",
                }
            ],
        },
        ("GET", "/api/v1/proxies"): {
            "items": [proxy_row(1), proxy_row(2, status="unknown", latency_ms=None)],
            "meta": {"total": 2, "limit": 200, "offset": 0},
        },
        ("GET", "/api/v1/proxies/stats"): {
            "total": 2,
            "by_status": {"healthy": 1, "unknown": 1},
            "healthy_with_latency": 1,
            "average_latency_ms": 37.0,
        },
        ("POST", "/api/v1/proxy-sources"): {
            "source_id": 1,
            "source_name": "Desktop import",
            "format_detected": "txt",
            "entries_seen": 2,
            "accepted": 2,
            "duplicates": 0,
            "rejected": 0,
            "rejected_samples": [],
            "credentials_refused": 0,
        },
        ("POST", "/api/v1/proxies/1/check"): {
            "proxy_id": 1,
            "ok": True,
            "latency_ms": 37,
            "http_status": 200,
            "error_class": None,
            "error_message": None,
            "status": "healthy",
        },
        ("POST", "/api/v1/proxies/check-batch"): {
            "job_id": "job-1",
            "job_type": "proxy.check_batch",
            "proxy_count": 2,
        },
        ("GET", "/api/v1/inboxes"): {
            "items": [inbox_row("inbox-1"), inbox_row("inbox-2", status="expired")],
            "meta": {"total": 2, "limit": 200, "offset": 0},
        },
        ("GET", "/api/v1/email-domains"): [
            {
                "id": 1,
                "domain": "example.test",
                "is_reserved": True,
                "owner_verified": False,
                "is_active": True,
                "description": None,
                "inbox_count": 2,
            }
        ],
        ("POST", "/api/v1/inboxes"): inbox_row("inbox-3"),
        ("POST", "/api/v1/inboxes/bulk"): {
            "created": 2,
            "inboxes": [inbox_row("a"), inbox_row("b")],
        },
        ("POST", "/api/v1/inboxes/cleanup"): {"expired": 1},
        ("GET", "/api/v1/inboxes/inbox-1/messages"): {
            "items": [message_row(1)],
            "meta": {"total": 1, "limit": 100, "offset": 0},
        },
        ("POST", "/api/v1/inboxes/inbox-1/poll"): {
            "inbox": "qa-inbox-1@example.test",
            "ingested": 1,
            "skipped": 0,
            "already_known": 0,
        },
        ("GET", "/api/v1/messages/1"): {
            **message_row(1),
            "body_text": "Your verification code is 481920",
            "body_html": "<p>Your verification code is <b>481920</b></p>",
            "headers": {},
        },
        ("POST", "/api/v1/messages/1/extract-code"): {
            "message_id": 1,
            "code": "481920",
            "confidence": 0.9,
            "strategy": "labelled:text",
            "candidates": [],
        },
        ("GET", "/api/v1/targets"): [target_row(1)],
        ("POST", "/api/v1/targets"): target_row(2),
        ("POST", "/api/v1/targets/1/authorize"): target_row(1),
        ("GET", "/api/v1/campaigns"): {
            "items": [campaign_row("campaign-1")],
            "meta": {"total": 1, "limit": 50, "offset": 0},
        },
        ("POST", "/api/v1/campaigns"): {
            "campaign": campaign_row("campaign-2", status="queued"),
            "job_id": "job-2",
        },
        ("POST", "/api/v1/campaigns/campaign-1/cancel"): campaign_row(
            "campaign-1", status="cancelled", stop_reason="cancelled by operator"
        ),
        ("GET", "/api/v1/campaigns/campaign-1/results"): {
            "campaign": campaign_row("campaign-1"),
            "items": [
                {
                    "test_id": 1,
                    "email": "qa-inbox-1@example.test",
                    "status": "active",
                    "outcome": "success",
                    "verification_code": "481920",
                    "code_extraction_confidence": 0.9,
                    "account_status": "active",
                    "duration_ms": 412,
                    "error_message": None,
                    "steps": {"submit": "accepted"},
                }
            ],
            "meta": {"total": 1, "limit": 200, "offset": 0},
            "summary": {"success": 1, "product_defects": 0, "controls_encountered": 0},
        },
        ("GET", "/api/v1/jobs"): {
            "items": [job_row("job-1"), job_row("job-2", status="failed", error_message="boom")],
            "meta": {"total": 2, "limit": 100, "offset": 0},
        },
        ("GET", "/api/v1/jobs/job-1"): {
            **job_row("job-1"),
            "logs": [
                {"level": "info", "message": "checked 1/3", "created_at": "2026-11-16T04:00:00Z"}
            ],
            "payload": {"proxy_ids": [1, 2], "password": "***REDACTED***"},
        },
        ("POST", "/api/v1/jobs/job-1/cancel"): {
            "public_id": "job-1",
            "status": "running",
            "cancel_requested": True,
        },
    }


class FakeApi:
    """Routes requests to canned responses and records what was called."""

    def __init__(self, routes: dict[tuple[str, str], Json] | None = None) -> None:
        self.routes: dict[tuple[str, str], Json] = routes or default_routes()
        self.calls: list[tuple[str, str]] = []
        self.text_routes: dict[tuple[str, str], str] = {
            (
                "GET",
                "/api/v1/proxies/export",
            ): "protocol,host,port\nhttp,proxy1.example.test,8080\n",
            ("GET", "/api/v1/inboxes/inbox-1/export"): "id,sender\n1,no-reply@demo.test\n",
            ("GET", "/api/v1/analytics/export"): "public_id,job_type\njob-1,proxy.check_batch\n",
        }

    def transport(self) -> httpx.MockTransport:
        """Return a transport that serves this fake."""

        def handler(request: httpx.Request) -> httpx.Response:
            key = (request.method, request.url.path)
            self.calls.append(key)
            if key in self.text_routes:
                return httpx.Response(
                    200,
                    content=self.text_routes[key].encode(),
                    headers={"content-type": "text/csv"},
                )
            if key in self.routes:
                return httpx.Response(200, json=self.routes[key])
            return httpx.Response(
                404,
                json={"code": "not_found", "message": f"No route for {key}", "request_id": "test"},
            )

        return httpx.MockTransport(handler)

    def json_body(self, request: httpx.Request) -> Json:
        """Decode a request body (helper for tests that assert payloads)."""
        return json.loads(request.content.decode() or "{}")
