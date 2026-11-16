"""Client-side activity log.

Records what *this client* did and what came back, which is what someone debugging a UI problem
actually needs: the backend has its own logs, keyed by request id, and the status bar only shows
the last message.

Deliberately in memory and bounded. A desktop client writing an unbounded log file it never
rotates is how disk-space problems start, and the backend already keeps the durable record.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, Signal

#: How many entries are kept. Enough for a session's worth of actions, bounded by construction.
MAX_ENTRIES = 1000


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One client-side event."""

    timestamp: datetime
    level: str
    message: str

    @property
    def time_text(self) -> str:
        """Local time, seconds resolution."""
        return self.timestamp.astimezone().strftime("%H:%M:%S")


class ActivityLog(QObject):
    """A bounded, observable log of client activity."""

    appended = Signal(object)

    def __init__(self, *, max_entries: int = MAX_ENTRIES) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=max_entries)

    def add(self, level: str, message: str) -> LogEntry:
        """Record an entry and notify listeners."""
        entry = LogEntry(timestamp=datetime.now().astimezone(), level=level, message=message)
        self._entries.append(entry)
        self.appended.emit(entry)
        return entry

    def info(self, message: str) -> LogEntry:
        """Record an informational entry."""
        return self.add("info", message)

    def error(self, message: str) -> LogEntry:
        """Record an error entry."""
        return self.add("error", message)

    def warning(self, message: str) -> LogEntry:
        """Record a warning entry."""
        return self.add("warning", message)

    def clear(self) -> None:
        """Discard every entry."""
        self._entries.clear()

    @property
    def entries(self) -> list[LogEntry]:
        """The current entries, oldest first."""
        return list(self._entries)


__all__ = ["MAX_ENTRIES", "ActivityLog", "LogEntry"]
