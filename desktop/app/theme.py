"""Dark theme: palette and stylesheet.

Colours are defined once as tokens and referenced by the stylesheet and by the table models, so a
status colour cannot drift between the two. The palette is applied to the application *and* the
stylesheet is applied, because Qt widgets use the palette for some drawing (menus, tooltips) and
the stylesheet for the rest - setting only one produces the half-themed look everyone recognises.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

#: Colour tokens. Names describe the role, not the hue, so the theme can change without renaming.
COLORS: dict[str, str] = {
    "bg": "#14161a",
    "bg_alt": "#1a1d22",
    "surface": "#21252b",
    "surface_alt": "#282d34",
    "border": "#31363f",
    "text": "#e6e8eb",
    "text_muted": "#9aa3ad",
    "accent": "#4c8dff",
    "accent_muted": "#2f5ea8",
    "success": "#3fb950",
    "warning": "#d29922",
    "danger": "#f85149",
    "info": "#58a6ff",
    "neutral": "#8b949e",
}

#: Status string -> colour token. Shared by every table so one status looks the same everywhere.
STATUS_COLORS: dict[str, str] = {
    # proxies
    "healthy": "success",
    "unknown": "neutral",
    "unhealthy": "warning",
    "expired": "danger",
    # inboxes
    "active": "success",
    "archived": "neutral",
    # jobs and campaigns
    "queued": "info",
    "running": "accent",
    "completed": "success",
    "failed": "danger",
    "cancelled": "warning",
    "stopped_by_target": "warning",
    # tests
    "pending": "neutral",
    "submitted": "info",
    "awaiting_email": "info",
    "verified": "accent",
    "blocked_by_target": "warning",
    # outcomes
    "success": "success",
    "verification_failed": "danger",
    "email_timeout": "warning",
    "validation_error": "warning",
    "unexpected_error": "danger",
    "rate_limited": "warning",
    "captcha_encountered": "warning",
    "access_denied": "danger",
}


def color_for_status(status: str) -> QColor:
    """Return the colour for a status string, falling back to the neutral token."""
    token = STATUS_COLORS.get((status or "").lower(), "neutral")
    return QColor(COLORS[token])


def build_stylesheet() -> str:
    """Return the application stylesheet."""
    c = COLORS
    return f"""
    QWidget {{
        background-color: {c["bg"]};
        color: {c["text"]};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background-color: {c["bg"]}; }}

    /* --- navigation rail --------------------------------------------------------- */
    QListWidget#Nav {{
        background-color: {c["bg_alt"]};
        border: none;
        border-right: 1px solid {c["border"]};
        outline: none;
        padding: 8px 6px;
    }}
    QListWidget#Nav::item {{
        padding: 10px 12px;
        border-radius: 6px;
        color: {c["text_muted"]};
    }}
    QListWidget#Nav::item:selected {{
        background-color: {c["surface"]};
        color: {c["text"]};
        border-left: 3px solid {c["accent"]};
    }}
    QListWidget#Nav::item:hover {{ background-color: {c["surface_alt"]}; }}

    /* --- surfaces ---------------------------------------------------------------- */
    QFrame#Card, QFrame#Panel {{
        background-color: {c["surface"]};
        border: 1px solid {c["border"]};
        border-radius: 8px;
    }}
    QLabel#CardTitle {{ color: {c["text_muted"]}; font-size: 12px; }}
    QLabel#CardValue {{ font-size: 26px; font-weight: 600; }}
    QLabel#CardHint  {{ color: {c["text_muted"]}; font-size: 11px; }}
    QLabel#PageTitle {{ font-size: 20px; font-weight: 600; }}
    QLabel#PageSubtitle {{ color: {c["text_muted"]}; }}
    QLabel#Muted {{ color: {c["text_muted"]}; }}

    /* --- controls ---------------------------------------------------------------- */
    QPushButton {{
        background-color: {c["surface_alt"]};
        border: 1px solid {c["border"]};
        border-radius: 6px;
        padding: 6px 12px;
    }}
    QPushButton:hover {{ border-color: {c["accent"]}; }}
    QPushButton:pressed {{ background-color: {c["surface"]}; }}
    QPushButton:disabled {{ color: {c["text_muted"]}; border-color: {c["border"]}; }}
    QPushButton#Primary {{
        background-color: {c["accent_muted"]};
        border-color: {c["accent"]};
        font-weight: 600;
    }}
    QPushButton#Danger {{ border-color: {c["danger"]}; }}

    QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
        background-color: {c["bg_alt"]};
        border: 1px solid {c["border"]};
        border-radius: 6px;
        padding: 5px 8px;
        selection-background-color: {c["accent_muted"]};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {c["accent"]}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}

    /* --- tables ------------------------------------------------------------------ */
    QTableView, QTreeView {{
        background-color: {c["surface"]};
        alternate-background-color: {c["bg_alt"]};
        gridline-color: {c["border"]};
        border: 1px solid {c["border"]};
        border-radius: 8px;
        selection-background-color: {c["accent_muted"]};
        selection-color: {c["text"]};
    }}
    QHeaderView::section {{
        background-color: {c["bg_alt"]};
        color: {c["text_muted"]};
        border: none;
        border-bottom: 1px solid {c["border"]};
        padding: 6px 8px;
    }}
    QTableView::item {{ padding: 4px 6px; }}

    /* --- misc -------------------------------------------------------------------- */
    QProgressBar {{
        background-color: {c["bg_alt"]};
        border: 1px solid {c["border"]};
        border-radius: 6px;
        text-align: center;
        height: 16px;
    }}
    QProgressBar::chunk {{ background-color: {c["accent"]}; border-radius: 5px; }}
    QStatusBar {{ background-color: {c["bg_alt"]}; border-top: 1px solid {c["border"]}; }}
    QToolTip {{
        background-color: {c["surface_alt"]};
        color: {c["text"]};
        border: 1px solid {c["border"]};
    }}
    QScrollBar:vertical {{ background: {c["bg_alt"]}; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {c["border"]}; border-radius: 5px; min-height: 24px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QSplitter::handle {{ background-color: {c["border"]}; }}
    """


def build_palette() -> QPalette:
    """Return a dark palette for the application."""
    palette = QPalette()
    c = COLORS
    palette.setColor(QPalette.ColorRole.Window, QColor(c["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(c["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c["bg_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(c["surface_alt"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c["accent_muted"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["surface_alt"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["text_muted"]))
    palette.setColor(QPalette.ColorRole.Link, QColor(c["info"]))
    return palette


def apply_theme(target: QApplication | QWidget) -> None:
    """Apply the dark palette and stylesheet.

    Accepts a ``QApplication`` (the normal case) or a widget, which is what tests use to check a
    single view without standing up an application.
    """
    if isinstance(target, QApplication):
        target.setStyle("Fusion")  # the native style ignores large parts of a stylesheet
        target.setPalette(build_palette())
    target.setStyleSheet(build_stylesheet())


__all__ = [
    "COLORS",
    "STATUS_COLORS",
    "apply_theme",
    "build_palette",
    "build_stylesheet",
    "color_for_status",
    "Qt",
]
