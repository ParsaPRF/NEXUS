"""Core cross-cutting concerns: configuration, logging, security, networking guard.

Nothing in this package may import from ``app.modules``, ``app.services`` or ``app.api``.
That rule is what keeps the dependency arrows pointing downwards (see docs/ARCHITECTURE.md 6).
"""

from app.core.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
