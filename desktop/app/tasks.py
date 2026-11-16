"""Off-thread execution for API calls.

Qt's event loop and the network do not mix: a blocking call on the GUI thread freezes the window,
which is the specific failure the brief calls out ("the interface must remain responsive while
background jobs execute"). Every API call therefore runs on a :class:`QThreadPool` worker thread
and reports back through Qt signals, which Qt delivers on the GUI thread.

Two rules make this safe:

* worker threads never touch widgets - they only emit signals carrying plain Python data;
* the caller keeps a reference to the returned :class:`Task`, because a ``QRunnable`` that is
  garbage-collected mid-flight is a crash waiting to happen (the task itself holds the signals
  object alive).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from app.api_client import ApiError, NexusApiError


class TaskSignals(QObject):
    """Signals emitted by a background task."""

    started = Signal()
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()


class Task(QRunnable):
    """Runs one callable on a worker thread and reports the outcome."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.signals = TaskSignals()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        """Execute the callable; every exception becomes a ``failed`` signal."""
        self.signals.started.emit()
        try:
            result = self._fn(*self._args, **self._kwargs)
        except NexusApiError as exc:
            self.signals.failed.emit(exc.error)
        except Exception as exc:  # noqa: BLE001 - a worker thread must never propagate
            self.signals.failed.emit(
                ApiError(code="client_error", message=f"{type(exc).__name__}: {exc}")
            )
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


class TaskRunner:
    """Submits tasks to a shared thread pool."""

    def __init__(self, max_threads: int = 4) -> None:
        self._pool = QThreadPool()
        # A small pool: this is a local tool driving a local API, and unbounded fan-out is the
        # behaviour the project refuses everywhere else.
        self._pool.setMaxThreadCount(max(1, max_threads))
        self._active: list[Task] = []

    @property
    def active_count(self) -> int:
        """Number of tasks that have not reported back yet."""
        return len(self._active)

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[ApiError], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> Task:
        """Queue ``fn`` for execution and wire up its callbacks."""
        task = Task(fn, *args, **kwargs)
        self._active.append(task)

        if on_success is not None:
            task.signals.succeeded.connect(on_success)
        if on_error is not None:
            task.signals.failed.connect(on_error)
        if on_finished is not None:
            task.signals.finished.connect(on_finished)

        def _release() -> None:
            """Drop our reference once the task is done."""
            if task in self._active:
                self._active.remove(task)

        task.signals.finished.connect(_release)
        self._pool.start(task)
        return task

    def wait_for_done(self, timeout_ms: int = 10_000) -> bool:
        """Block until every queued task finishes. Used by tests and by shutdown."""
        return self._pool.waitForDone(timeout_ms)

    def shutdown(self) -> None:
        """Stop accepting work and wait briefly for running tasks."""
        self._pool.clear()
        self._pool.waitForDone(2_000)
