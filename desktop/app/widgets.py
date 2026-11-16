"""Shared widgets: table model, stat cards, page headers.

Two ideas are reused by every view, so they live here rather than being re-implemented seven times
with seven small differences:

* :class:`DictTableModel` renders a list of dictionaries. The API returns JSON objects, so a model
  that consumes dictionaries directly removes a mapping layer per view - and a mapping layer per
  view is where columns quietly drift out of sync with the API.
* :class:`StatCard` is the dashboard's unit of information: a label, a big number and a hint.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.theme import COLORS, color_for_status


@dataclass(frozen=True, slots=True)
class Column:
    """One table column: which JSON key it shows, its header, and how to render it."""

    key: str
    header: str
    width: int = 120
    formatter: Callable[[Any], str] | None = None
    colorize: bool = False
    """When true, the cell text is coloured by status (see theme.STATUS_COLORS)."""


class DictTableModel(QAbstractTableModel):
    """A read-only table model over a list of dictionaries."""

    def __init__(self, columns: Sequence[Column], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[dict[str, Any]] = []

    # -- Qt overrides -----------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        """Number of rows (0 for an index, since this is a flat table)."""
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        """Number of columns."""
        if parent is not None and parent.isValid():
            return 0
        return len(self._columns)

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        """Column headers."""
        if orientation is not Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._columns[section].header
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Cell contents and (for status columns) their colour."""
        if not index.isValid():
            return None
        column = self._columns[index.column()]
        row = self._rows[index.row()]
        value = row.get(column.key)

        if role == Qt.ItemDataRole.DisplayRole:
            if column.formatter is not None:
                return column.formatter(value)
            if value is None:
                return ""
            if isinstance(value, bool):
                return "yes" if value else "no"
            if isinstance(value, float):
                return f"{value:.1f}"
            return str(value)

        if role == Qt.ItemDataRole.ForegroundRole and column.colorize:
            return color_for_status(str(value or ""))
        if role == Qt.ItemDataRole.ForegroundRole and value in (None, ""):
            return QColor(COLORS["text_muted"])
        if role == Qt.ItemDataRole.ToolTipRole and value not in (None, ""):
            return str(value)
        return None

    # -- data access ------------------------------------------------------------------

    def set_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        """Replace every row and notify the view."""
        self.beginResetModel()
        self._rows = [dict(row) for row in rows]
        self.endResetModel()

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The current rows (used by views and tests)."""
        return self._rows

    def row_at(self, index: QModelIndex) -> dict[str, Any] | None:
        """Return the row behind a view index."""
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        return self._rows[index.row()]

    def value(self, row_index: int, key: str) -> Any:
        """Return one cell by row position and key."""
        if 0 <= row_index < len(self._rows):
            return self._rows[row_index].get(key)
        return None


class StatCard(QFrame):
    """A dashboard tile: title, value and a short hint."""

    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)

        self._title = QLabel(title)
        self._title.setObjectName("CardTitle")
        self._value = QLabel("—")
        self._value.setObjectName("CardValue")
        self._hint = QLabel(hint)
        self._hint.setObjectName("CardHint")

        layout.addWidget(self._title)
        layout.addWidget(self._value)
        layout.addWidget(self._hint)

    def set_value(self, value: Any, hint: str | None = None) -> None:
        """Update the tile's number and, optionally, its hint."""
        self._value.setText("—" if value is None else str(value))
        if hint is not None:
            self._hint.setText(hint)

    def set_color(self, token: str) -> None:
        """Tint the value with a colour token (``success``, ``danger``, …)."""
        self._value.setStyleSheet(f"color: {COLORS.get(token, COLORS['text'])};")

    @property
    def value_text(self) -> str:
        """The displayed value, for tests."""
        return self._value.text()


def page_header(title: str, subtitle: str = "") -> tuple[QWidget, QLabel]:
    """Build a page header and return it with its subtitle label.

    The label is returned so a view can update the subtitle with a count or a timestamp.
    """
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)

    title_label = QLabel(title)
    title_label.setObjectName("PageTitle")
    layout.addWidget(title_label)

    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("PageSubtitle")
    layout.addWidget(subtitle_label)
    return container, subtitle_label


def button(text: str, *, primary: bool = False, danger: bool = False) -> QPushButton:
    """Create a themed push button."""
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("Primary")
    elif danger:
        widget.setObjectName("Danger")
    return widget


def toolbar(*widgets: QWidget) -> QWidget:
    """Lay widgets out in a horizontal toolbar with a trailing stretch."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return container


def muted(text: str) -> QLabel:
    """Create a muted, secondary label."""
    label = QLabel(text)
    label.setObjectName("Muted")
    return label


__all__ = [
    "Column",
    "DictTableModel",
    "StatCard",
    "button",
    "muted",
    "page_header",
    "toolbar",
]
