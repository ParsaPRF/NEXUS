"""In-process job queue tests.

The queue is deliberately simple in Phase 1: no persistence, no distributed workers. What it
must get right is the behaviour every later backend has to preserve:

* an unknown job type is refused at enqueue time, not silently dropped inside a worker;
* the payload is redacted before the job sees it;
* cancellation is cooperative and a job cannot ignore it;
* a job that checks no cancellation points cannot hang shutdown forever.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import (
    JobCancelledError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import REDACTED
from app.jobs.queue import (
    InProcessJobQueue,
    get_handler,
    get_queue,
    register_handler,
    registered_job_types,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Give each test its own handler registry.

    The registry is module-level and global, which is correct in production (handlers are
    registered once at import) but would let one test's handler leak into another's.
    """
    from app.jobs import queue as queue_module

    monkeypatch.setattr(queue_module, "_HANDLERS", {})


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


class TestHandlerRegistry:
    """Job types are resolved by name, which is what a Redis-backed queue needs too."""

    def test_registered_handler_is_retrievable(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            return None

        register_handler("test.noop", handler)
        assert get_handler("test.noop") is handler
        assert "test.noop" in registered_job_types()

    def test_unknown_job_type_raises_not_found(self) -> None:
        with pytest.raises(NotFoundError, match="No handler is registered"):
            get_handler("test.does_not_exist")

    def test_empty_job_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="name is required"):
            register_handler("", lambda *_: None)  # type: ignore[arg-type]

    def test_re_registering_the_same_handler_is_allowed(self) -> None:
        # Idempotent re-registration is needed because a module can be imported twice under
        # different names in some test setups.
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            return None

        register_handler("test.same", handler)
        register_handler("test.same", handler)
        assert get_handler("test.same") is handler

    def test_conflicting_registration_is_refused(self) -> None:
        # Two modules fighting over one job name is a bug worth failing loudly on.
        async def first(context, payload):  # type: ignore[no-untyped-def]
            return None

        async def second(context, payload):  # type: ignore[no-untyped-def]
            return None

        register_handler("test.conflict", first)
        with pytest.raises(ValidationError, match="already registered"):
            register_handler("test.conflict", second)


# --------------------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------------------


class TestEnqueue:
    """Enqueueing validates before accepting work."""

    async def test_unknown_job_type_fails_at_enqueue(self) -> None:
        queue = InProcessJobQueue()
        # Failing here means the API can return 404 to the caller. Accepting the job and losing
        # it inside a background task would be far worse.
        with pytest.raises(NotFoundError):
            await queue.enqueue("test.missing")

    async def test_enqueue_returns_a_unique_id(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            return None

        register_handler("test.noop", handler)
        queue = InProcessJobQueue()
        first = await queue.enqueue("test.noop")
        second = await queue.enqueue("test.noop")
        assert first != second
        await queue.shutdown()

    async def test_job_runs_and_completes(self) -> None:
        seen: list[str] = []

        async def handler(context, payload):  # type: ignore[no-untyped-def]
            seen.append(payload["value"])
            return {"ok": True}

        register_handler("test.record", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.record", {"value": "hello"})

        for _ in range(50):
            status = await queue.get_status(job_id)
            if status.status == "completed":
                break
            await asyncio.sleep(0.01)

        assert seen == ["hello"]
        assert (await queue.get_status(job_id)).status == "completed"
        await queue.shutdown()

    async def test_payload_is_redacted_before_the_job_sees_it(self) -> None:
        captured: dict[str, object] = {}

        async def handler(context, payload):  # type: ignore[no-untyped-def]
            captured.update(payload)
            return

        register_handler("test.redact", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue(
            "test.redact", {"password": "hunter2", "password_hint": "not-a-secret"}
        )
        for _ in range(50):
            if (await queue.get_status(job_id)).status == "completed":
                break
            await asyncio.sleep(0.01)

        assert captured["password"] == REDACTED
        assert "hunter2" not in str(captured)
        await queue.shutdown()

    async def test_enqueue_after_shutdown_is_refused(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            return None

        register_handler("test.noop", handler)
        queue = InProcessJobQueue()
        await queue.shutdown()
        with pytest.raises(ValidationError, match="shut down"):
            await queue.enqueue("test.noop")


# --------------------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------------------


class TestFailureHandling:
    """A failing job is recorded as failed and never takes the process down."""

    async def test_failing_job_is_marked_failed(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            msg = "simulated failure"
            raise RuntimeError(msg)

        register_handler("test.fail", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.fail")

        for _ in range(50):
            if (await queue.get_status(job_id)).status in {"failed", "completed"}:
                break
            await asyncio.sleep(0.01)

        status = await queue.get_status(job_id)
        assert status.status == "failed"
        # The message records the exception *type*, not the message: an exception message can
        # contain user data or a connection string.
        assert "RuntimeError" in (status.message or "")
        await queue.shutdown()

    async def test_status_of_unknown_job_raises(self) -> None:
        queue = InProcessJobQueue()
        with pytest.raises(NotFoundError):
            await queue.get_status("no-such-job")


# --------------------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------------------


class TestCancellation:
    """Cancellation is a request the job honours at a safe point."""

    async def test_cooperative_cancellation_stops_the_job(self) -> None:
        processed: list[int] = []

        async def handler(context, payload):  # type: ignore[no-untyped-def]
            context.set_total(100)
            for index in range(100):
                context.check_cancelled()
                processed.append(index)
                context.advance()
                await asyncio.sleep(0.005)
            return {"processed": len(processed)}

        register_handler("test.long", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.long")

        await asyncio.sleep(0.05)
        assert await queue.cancel(job_id) is True
        await asyncio.sleep(0.1)

        assert (await queue.get_status(job_id)).status == "cancelled"
        # It must have stopped early, not run to completion.
        assert len(processed) < 100
        await queue.shutdown()

    async def test_cancelling_an_unknown_job_returns_false(self) -> None:
        queue = InProcessJobQueue()
        assert await queue.cancel("no-such-job") is False

    async def test_cancelling_a_finished_job_returns_false(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            return None

        register_handler("test.quick", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.quick")
        for _ in range(50):
            if (await queue.get_status(job_id)).status == "completed":
                break
            await asyncio.sleep(0.01)

        assert await queue.cancel(job_id) is False
        await queue.shutdown()

    async def test_a_job_that_never_checks_cannot_block_shutdown_forever(self) -> None:
        """The forceful fallback exists so one badly written job cannot hang the process."""

        async def stubborn(context, payload):  # type: ignore[no-untyped-def]
            # Deliberately ignores cooperative cancellation.
            await asyncio.sleep(30)
            return

        register_handler("test.stubborn", stubborn)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.stubborn")
        await asyncio.sleep(0.05)

        # Must return well before the job's own 30 second sleep.
        cancelled = await asyncio.wait_for(queue.cancel(job_id), timeout=15)
        assert cancelled is True
        assert (await queue.get_status(job_id)).status == "cancelled"
        await queue.shutdown()

    async def test_shutdown_terminates_running_jobs(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            return

        register_handler("test.sleepy", handler)
        queue = InProcessJobQueue()
        await queue.enqueue("test.sleepy")
        await asyncio.sleep(0.05)

        await asyncio.wait_for(queue.shutdown(), timeout=15)


# --------------------------------------------------------------------------------------
# Concurrency and progress
# --------------------------------------------------------------------------------------


class TestConcurrencyAndProgress:
    """Concurrency is bounded and progress is reportable."""

    async def test_concurrency_limit_is_enforced(self) -> None:
        state = {"running": 0, "peak": 0}

        async def handler(context, payload):  # type: ignore[no-untyped-def]
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.05)
            state["running"] -= 1
            return

        register_handler("test.concurrent", handler)
        queue = InProcessJobQueue(max_concurrent_jobs=2)
        for _ in range(6):
            await queue.enqueue("test.concurrent")

        await asyncio.sleep(0.4)
        # Bounded fan-out matters: unbounded concurrency against a target is exactly the
        # behaviour the project refuses (threat T12).
        assert state["peak"] <= 2
        await queue.shutdown()

    async def test_invalid_concurrency_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InProcessJobQueue(max_concurrent_jobs=0)

    async def test_progress_is_reported(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            context.set_total(10)
            for index in range(10):
                context.set_progress(index + 1, message=f"step {index + 1}")
                await asyncio.sleep(0.005)
            return

        register_handler("test.progress", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.progress")

        await asyncio.sleep(0.02)
        midway = await queue.get_status(job_id)
        assert midway.progress_total == 10
        assert 0 < midway.progress_current <= 10

        for _ in range(100):
            if (await queue.get_status(job_id)).status == "completed":
                break
            await asyncio.sleep(0.01)

        final = await queue.get_status(job_id)
        assert final.progress_current == 10
        assert final.progress_percent == 100.0
        await queue.shutdown()

    async def test_progress_is_clamped(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            context.set_total(5)
            context.set_progress(999)  # a buggy job must not report 19980%
            return

        register_handler("test.clamp", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.clamp")
        for _ in range(50):
            if (await queue.get_status(job_id)).status == "completed":
                break
            await asyncio.sleep(0.01)

        assert (await queue.get_status(job_id)).progress_percent <= 100.0
        await queue.shutdown()

    async def test_active_job_ids_tracks_unfinished_work(self) -> None:
        async def handler(context, payload):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            return

        register_handler("test.hang", handler)
        queue = InProcessJobQueue()
        job_id = await queue.enqueue("test.hang")
        await asyncio.sleep(0.02)
        assert job_id in queue.active_job_ids
        await queue.shutdown()


# --------------------------------------------------------------------------------------
# Job context
# --------------------------------------------------------------------------------------


class TestJobContext:
    """The context object handed to handlers."""

    async def test_check_cancelled_raises_after_a_request(self) -> None:
        from app.jobs.queue import JobContext

        context = JobContext(job_id="j1", job_type="t", payload={})
        context.check_cancelled()  # no-op while not cancelled
        context.request_cancel()
        assert context.cancelled
        with pytest.raises(JobCancelledError):
            context.check_cancelled()

    async def test_log_messages_are_redacted(self) -> None:
        from app.jobs.queue import JobContext

        context = JobContext(job_id="j2", job_type="t", payload={})
        context.log("connecting with token=supersecret")
        assert "supersecret" not in "".join(context.messages)

    async def test_advance_increments_the_counter(self) -> None:
        from app.jobs.queue import JobContext

        context = JobContext(job_id="j3", job_type="t", payload={})
        context.set_total(3)
        context.advance()
        context.advance()
        assert context.progress_current == 2


# --------------------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------------------


class TestQueueFactory:
    """The queue is selected by configuration, and the not-yet-implemented backend says so."""

    async def test_local_profile_returns_the_in_process_queue(self, test_settings) -> None:
        from app.jobs.queue import reset_queue

        queue = get_queue(test_settings)
        assert isinstance(queue, InProcessJobQueue)
        await reset_queue()

    async def test_rq_backend_reports_that_it_is_not_implemented_yet(self, test_settings) -> None:
        # A silent fallback to the in-process runner would look like a working Redis setup.
        from app.core.exceptions import ConfigurationError
        from app.jobs.queue import reset_queue

        await reset_queue()
        settings = test_settings.model_copy(deep=True)
        settings.jobs.backend = "rq"
        with pytest.raises(ConfigurationError, match="Phase 5"):
            get_queue(settings)

    async def test_reset_clears_the_singleton(self, test_settings) -> None:
        from app.jobs.queue import reset_queue

        first = get_queue(test_settings)
        await reset_queue()
        assert get_queue(test_settings) is not first
        await reset_queue()
