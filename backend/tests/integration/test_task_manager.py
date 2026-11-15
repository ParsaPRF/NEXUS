"""Task Manager and analytics tests.

Covers the durable job lifecycle (queued → running → terminal), progress and logs, idempotent
enqueue, cooperative cancellation driven by the database flag, and the analytics endpoints.

Two synthetic handlers are registered here: a slow one (to have something cancellable) and a
progress-reporting one (to have something with logs and a duration).
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.database.engine import get_session_factory
from app.models.job_models import BackgroundJob
from app.modules.task_manager.service import JobService

# The synthetic handlers 'test.slow' and 'test.progress' are registered once in conftest.py.


async def _enqueue(settings, job_type: str, payload: dict | None = None, key: str | None = None):  # type: ignore[no-untyped-def]
    """Enqueue through the service with its own session, like a module would."""
    async with get_session_factory()() as session:
        return await JobService(session, settings).enqueue(
            job_type, payload or {}, idempotency_key=key
        )


async def _wait_for_status(
    client: AsyncClient, public_id: str, wanted: str, ticks: int = 300
) -> dict:
    """Poll the monitor until the job reaches ``wanted``."""
    job: dict = {}
    for _ in range(ticks):
        job = (await client.get(f"/api/v1/jobs/{public_id}")).json()
        if job["status"] == wanted:
            return job
        await asyncio.sleep(0.02)
    assert job.get("status") == wanted, job
    return job


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


class TestLifecycle:
    """A job is observable from creation to completion."""

    async def test_enqueue_creates_a_queued_job(self, client: AsyncClient, test_settings) -> None:
        job, _queue_id, created = await _enqueue(test_settings, "test.progress")
        assert created is True

        body = (await client.get(f"/api/v1/jobs/{job.public_id}")).json()
        assert body["job_type"] == "test.progress"
        assert body["status"] in {"queued", "running", "completed"}
        assert body["progress_total"] == 0 or body["progress_total"] == 3

    async def test_job_completes_with_progress_and_duration(
        self, client: AsyncClient, test_settings
    ) -> None:
        job, _queue_id, _created = await _enqueue(test_settings, "test.progress")
        done = await _wait_for_status(client, job.public_id, "completed")

        assert done["progress_current"] == 3
        assert done["progress_total"] == 3
        assert done["progress_percent"] == 100.0
        assert done["duration_ms"] is not None
        assert done["started_at"] is not None
        assert done["finished_at"] is not None

    async def test_logs_are_recorded(self, client: AsyncClient, test_settings) -> None:
        job, _queue_id, _created = await _enqueue(test_settings, "test.progress")
        await _wait_for_status(client, job.public_id, "completed")

        detail = (await client.get(f"/api/v1/jobs/{job.public_id}")).json()
        messages = [entry["message"] for entry in detail["logs"]]
        assert any("processed" in message for message in messages)

    async def test_payload_is_exposed_redacted(self, client: AsyncClient, test_settings) -> None:
        job, _queue_id, _created = await _enqueue(
            test_settings, "test.progress", {"password": "hunter2", "note": "fine"}
        )
        detail = (await client.get(f"/api/v1/jobs/{job.public_id}")).json()
        assert detail["payload"]["password"] == "***REDACTED***"
        assert detail["payload"]["note"] == "fine"

    async def test_jobs_are_listed_newest_first(self, client: AsyncClient, test_settings) -> None:
        first, _, _ = await _enqueue(test_settings, "test.progress")
        second, _, _ = await _enqueue(test_settings, "test.progress")

        listing = (await client.get("/api/v1/jobs")).json()
        ids = [item["public_id"] for item in listing["items"]]
        assert ids.index(second.public_id) < ids.index(first.public_id)

    async def test_list_filters_by_type(self, client: AsyncClient, test_settings) -> None:
        await _enqueue(test_settings, "test.progress")
        await _enqueue(test_settings, "test.slow")

        filtered = (await client.get("/api/v1/jobs", params={"job_type": "test.slow"})).json()
        assert filtered["meta"]["total"] == 1
        assert filtered["items"][0]["job_type"] == "test.slow"

    async def test_unknown_job_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/jobs/nope")).status_code == 404


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


class TestIdempotency:
    """A retried request must not do the work twice."""

    async def test_same_key_returns_the_same_job(self, client: AsyncClient, test_settings) -> None:
        first, _, created_first = await _enqueue(test_settings, "test.progress", key="retry-1")
        second, _, created_second = await _enqueue(test_settings, "test.progress", key="retry-1")

        assert created_first is True
        assert created_second is False
        assert first.id == second.id

        async with get_session_factory()() as session:
            count = (
                await session.execute(select(func.count()).select_from(BackgroundJob))
            ).scalar_one()
        # Exactly one row, so exactly one execution.
        assert count == 1

    async def test_different_keys_create_separate_jobs(
        self, client: AsyncClient, test_settings
    ) -> None:
        first, _, _ = await _enqueue(test_settings, "test.progress", key="a")
        second, _, _ = await _enqueue(test_settings, "test.progress", key="b")
        assert first.id != second.id

    async def test_no_key_allows_duplicates(self, client: AsyncClient, test_settings) -> None:
        # Without a key, two calls really are two requests; the key is what makes a retry safe.
        await _enqueue(test_settings, "test.progress")
        await _enqueue(test_settings, "test.progress")
        async with get_session_factory()() as session:
            count = (
                await session.execute(select(func.count()).select_from(BackgroundJob))
            ).scalar_one()
        assert count == 2


# --------------------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------------------


class TestCancellation:
    """Cancellation is cooperative and visible in the row."""

    async def test_cancel_a_running_job(self, client: AsyncClient, test_settings) -> None:
        job, _queue_id, _created = await _enqueue(test_settings, "test.slow")
        await _wait_for_status(client, job.public_id, "running")

        response = await client.post(f"/api/v1/jobs/{job.public_id}/cancel")
        assert response.status_code == 202
        assert response.json()["cancel_requested"] is True

        done = await _wait_for_status(client, job.public_id, "cancelled")
        assert done["cancel_requested"] is True
        # It stopped early rather than running all 100 steps.
        assert done["progress_current"] < 100

    async def test_cancelling_a_finished_job_is_conflict(
        self, client: AsyncClient, test_settings
    ) -> None:
        job, _queue_id, _created = await _enqueue(test_settings, "test.progress")
        await _wait_for_status(client, job.public_id, "completed")

        response = await client.post(f"/api/v1/jobs/{job.public_id}/cancel")
        assert response.status_code == 409

    async def test_cancel_unknown_job_is_404(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/jobs/nope/cancel")).status_code == 404


# --------------------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------------------


class TestAnalytics:
    """Dashboard counts and exports."""

    async def test_summary_has_every_module(self, client: AsyncClient, test_settings) -> None:
        await client.post("/api/v1/proxy-sources", json={"content": "p1.example.test:8080"})
        await client.post("/api/v1/inboxes", json={"label": "analytics"})
        await _enqueue(test_settings, "test.progress")

        body = (await client.get("/api/v1/analytics/summary")).json()
        assert set(body) >= {
            "jobs",
            "proxies",
            "inboxes",
            "campaigns",
            "total_jobs",
            "total_proxies",
            "total_inboxes",
            "active_inboxes",
            "healthy_proxies",
            "recent_activity",
        }
        assert body["total_proxies"] == 1
        assert body["total_inboxes"] == 1
        assert body["active_inboxes"] == 1

    async def test_recent_activity_is_populated(self, client: AsyncClient, test_settings) -> None:
        await client.post("/api/v1/proxy-sources", json={"content": "p1.example.test:8080"})
        body = (await client.get("/api/v1/analytics/summary")).json()
        assert any(item["action"] == "proxy.import" for item in body["recent_activity"])

    async def test_export_jobs_as_csv(self, client: AsyncClient, test_settings) -> None:
        await _enqueue(test_settings, "test.progress")
        response = await client.get(
            "/api/v1/analytics/export", params={"dataset": "jobs", "format": "csv"}
        )
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert response.text.splitlines()[0].startswith("public_id,job_type,status")

    async def test_export_jobs_as_json(self, client: AsyncClient, test_settings) -> None:
        await _enqueue(test_settings, "test.progress")
        response = await client.get(
            "/api/v1/analytics/export", params={"dataset": "jobs", "format": "json"}
        )
        assert response.status_code == 200
        assert response.json()[0]["job_type"] == "test.progress"

    async def test_export_campaigns(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/analytics/export", params={"dataset": "campaigns", "format": "json"}
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_unknown_dataset_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/analytics/export", params={"dataset": "passwords"})
        assert response.status_code == 422

    async def test_unknown_format_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/analytics/export", params={"dataset": "jobs", "format": "xml"}
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


class TestAuth:
    """Job and analytics routes require the API key."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/jobs"),
            ("get", "/api/v1/jobs/x"),
            ("post", "/api/v1/jobs/x/cancel"),
            ("get", "/api/v1/analytics/summary"),
            ("get", "/api/v1/analytics/export"),
        ],
    )
    async def test_requires_api_key(
        self, anonymous_client: AsyncClient, method: str, path: str
    ) -> None:
        assert (await getattr(anonymous_client, method)(path)).status_code == 401


# --------------------------------------------------------------------------------------
# Wrapper behaviour
# --------------------------------------------------------------------------------------


class TestTrackedWrapper:
    """A job enqueued without a database row still runs (it is simply untracked)."""

    async def test_untracked_payload_runs_the_plain_handler(self) -> None:
        from app.jobs.queue import InProcessJobQueue, get_handler

        handler = get_handler("test.progress")
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.progress", {})
        await asyncio.sleep(0.1)
        assert (await queue.get_status(job_id)).status in {"completed", "running"}
        await queue.shutdown()
        assert callable(handler)
