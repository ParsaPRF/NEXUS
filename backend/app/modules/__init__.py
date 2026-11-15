"""Feature modules.

Each sub-package owns one NEXUS module: its models (shared, in ``app.models``), schemas,
repository, services and API router. The import rule from docs/ARCHITECTURE.md 6 applies:
a module may use another module's *service* and *schemas*, never its internals.
"""

from app.modules.proxy_manager import router as proxy_router  # noqa: F401  (registers its router)

__all__ = ["proxy_router"]
