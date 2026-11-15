"""RQ backend tests, run against an in-process Redis substitute.

A real worker process needs Docker, which is not available here. ``fakeredis`` gives us the Redis
command surface in memory, so the adapter's job - enqueue, status mapping, cancellation - is
exercised for real rather than being asserted from documentation.

What this does **not** prove: that an RQ worker process executes jobs against a real Redis. That
is listed as unverified in docs/ROADMAP.md until Phase 7, and the tests say so instead of
pretending otherwise.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ConfigurationError, NotFoundError
from app.jobs.rq_queue import RqJobQueue

fakeredis = pytest.importorskip("fakeredis", reason="fakeredis is a dev dependency")


@pytest.fixture
def rq_connection():
    """An in-memory Redis connection."""
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def rq_settings(test_settings):
    """Settings pointing at the RQ backend."""
    settings = test_settings.model_copy(deep=True)
    settings.jobs.backend = "rq"
    return settings


@pytest.fixture
def rq_queue(rq_settings, rq_connection):
    """An RQ adapter bound to the fake connection."""
    return RqJobQueue(rq_settings, connection=rq_connection)


class TestEnqueue:
    """Enqueueing puts a job on the queue with a stable id."""

    async def test_enqueue_returns_an_id(self, rq_queue) -> None:
        job_id = await rq_queue.enqueue("test.progress", {"job_db_id": 1})
        assert isinstance(job_id, str)
        assert job_id

    async def test_job_is_on_the_queue(self, rq_queue, rq_connection) -> None:
        await rq_queue.enqueue("test.progress", {"job_db_id": 1})
        assert len(rq_connection.keys("rq:*")) > 0

    async def test_payload_is_preserved(self, rq_queue, rq_connection) -> None:
        job_id = await rq_queue.enqueue("test.progress", {"job_db_id": 7, "extra": "x"})

        from rq.job import Job

        job = Job.fetch(job_id, connection=rq_connection)
        # RQ stores the arguments the dispatcher will be called with.
        assert job.args[0] == "test.progress"
        assert job.args[1]["job_db_id"] == 7

    async def test_two_enqueues_are_distinct_jobs(self, rq_queue) -> None:
        first = await rq_queue.enqueue("test.progress", {})
        second = await rq_queue.enqueue("test.progress", {})
        assert first != second


class TestStatus:
    """RQ statuses are mapped onto the NEXUS vocabulary."""

    async def test_queued_job_maps_to_queued(self, rq_queue) -> None:
        job_id = await rq_queue.enqueue("test.progress", {})
        snapshot = await rq_queue.get_status(job_id)
        assert snapshot.status == "queued"
        assert snapshot.job_id == job_id

    async def test_unknown_job_raises_not_found(self, rq_queue) -> None:
        with pytest.raises(NotFoundError):
            await rq_queue.get_status("does-not-exist")

    async def test_finished_job_maps_to_completed(self, rq_queue, rq_connection) -> None:
        from rq.job import Job

        job_id = await rq_queue.enqueue("test.progress", {})
        job = Job.fetch(job_id, connection=rq_connection)
        job.set_status("finished")

        snapshot = await rq_queue.get_status(job_id)
        assert snapshot.status == "completed"

    async def test_failed_job_maps_to_failed(self, rq_queue, rq_connection) -> None:
        from rq.job import Job

        job_id = await rq_queue.enqueue("test.progress", {})
        job = Job.fetch(job_id, connection=rq_connection)
        job.set_status("failed")

        assert (await rq_queue.get_status(job_id)).status == "failed"


class TestCancel:
    """Cancellation asks RQ to stop the job."""

    async def test_cancel_queued_job(self, rq_queue) -> None:
        job_id = await rq_queue.enqueue("test.progress", {})
        assert await rq_queue.cancel(job_id) is True
        assert (await rq_queue.get_status(job_id)).status == "cancelled"

    async def test_cancel_unknown_job_returns_false(self, rq_queue) -> None:
        assert await rq_queue.cancel("does-not-exist") is False

    async def test_cancel_finished_job_returns_false(self, rq_queue, rq_connection) -> None:
        from rq.job import Job

        job_id = await rq_queue.enqueue("test.progress", {})
        Job.fetch(job_id, connection=rq_connection).set_status("finished")
        assert await rq_queue.cancel(job_id) is False


class TestFactory:
    """Configuration selects the backend."""

    async def test_rq_backend_builds_the_adapter(self, rq_settings) -> None:
        from app.jobs.queue import get_queue, reset_queue

        await reset_queue()
        queue = get_queue(rq_settings)
        assert isinstance(queue, RqJobQueue)
        await reset_queue()

    def test_missing_extra_reports_the_install_command(
        self, monkeypatch: pytest.MonkeyPatch, rq_settings
    ) -> None:
        # Simulate the optional packages being absent: the error must say what to install.
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object):
            if name in {"redis", "rq"}:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ConfigurationError, match="--extra queue"):
            RqJobQueue(rq_settings)


class TestWorkerDispatcher:
    """The sync dispatcher runs an async handler (no worker process required)."""

    async def test_dispatches_a_handler(self, db_engine: object, test_settings) -> None:
        from app.workers.tasks import run_tracked_job

        # No job_db_id: the handler runs untracked, which is the documented degradation.
        result = await __import__("asyncio").to_thread(
            run_tracked_job, "test.progress", {}, test_settings
        )
        assert result == {"processed": 3}
