"""NEXUS desktop client (PySide6).

A pure API client: it talks to the NEXUS backend over HTTP and imports nothing from
``backend/app``. See docs/DECISION_LOG.md D7 for why that boundary is enforced by packaging rather
than by convention.
"""

__version__ = "0.1.0"
