"""Base class for views.

Every view follows the same shape: it holds the API client and the task runner, issues calls
through :meth:`BaseView.load`, and reports problems through the :attr:`BaseView.notify` signal so
the main window owns the one status bar. No view talks to another view.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from app.api_client import ApiError, NexusClient
from app.tasks import TaskRunner


class BaseView(QWidget):
    """Common behaviour for the module views."""

    #: ``(message, is_error)`` - emitted instead of showing a modal dialog for routine errors.
    notify = Signal(str, bool)

    #: Title shown in the window's header area.
    title = "View"
    subtitle = ""

    def __init__(
        self, client: NexusClient, runner: TaskRunner, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._runner = runner

    @property
    def client(self) -> NexusClient:
        """The API client this view talks to."""
        return self._client

    @property
    def runner(self) -> TaskRunner:
        """The thread pool this view submits work to."""
        return self._runner

    def refresh(self) -> None:
        """(Re)load this view's data. Subclasses override."""

    def load(
        self,
        call: Callable[[], Any],
        on_success: Callable[[Any], None],
        *,
        on_error: Callable[[ApiError], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        busy: Callable[[bool], None] | None = None,
    ) -> None:
        """Run an API call off the GUI thread and route the result.

        The default error handler reports through :attr:`notify`; a view can pass its own to add
        context, and can still chain the base behaviour by calling ``super()``-style handlers.
        """
        if busy is not None:
            busy(True)

        def _finished() -> None:
            if busy is not None:
                busy(False)
            if on_finished is not None:
                on_finished()

        self._runner.submit(
            call,
            on_success=on_success,
            on_error=on_error or self.report_error,
            on_finished=_finished,
        )

    def report_error(self, error: ApiError) -> None:
        """Surface an API failure in the status bar."""
        self.notify.emit(str(error), True)

    def report(self, message: str) -> None:
        """Surface an informational message in the status bar."""
        self.notify.emit(message, False)

    def apply_busy(self, widget: Any, busy: bool) -> None:
        """Disable a control while its call is in flight, so a double click cannot double-submit."""
        if widget is not None:
            widget.setEnabled(not busy)


__all__ = ["BaseView"]
