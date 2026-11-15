"""Pydantic schemas - the shapes that cross the API boundary.

Why separate schemas from models?
---------------------------------
The ORM model describes *storage*; a schema describes *the contract*. They differ constantly:
``password_hash`` must never be serialised, ``reliability`` is computed, timestamps need
formatting. Keeping them apart means a database change cannot accidentally change the public
API, and it is impossible to leak a column by forgetting to exclude it.

Only the schemas needed by the Phase 1 foundation live here. Module-specific schemas arrive
with their modules, each in ``app/modules/<name>/schemas.py``.
"""

from app.schemas.common import (
    ErrorResponse,
    HealthResponse,
    Page,
    PageMeta,
    ReadinessCheck,
    ReadinessResponse,
)

__all__ = [
    "ErrorResponse",
    "HealthResponse",
    "Page",
    "PageMeta",
    "ReadinessCheck",
    "ReadinessResponse",
]
