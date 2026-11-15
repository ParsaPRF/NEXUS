"""Business services.

A service is where a *use case* lives: it owns the transaction boundary, enforces business
rules, and coordinates repositories and adapters. It raises domain errors from
:mod:`app.core.exceptions`, never ``HTTPException`` - that translation belongs to the API layer,
which is what keeps services reusable by background workers.

Only the audit service exists in Phase 1. The module services arrive with their modules.
"""

from app.services.audit_service import AuditService

__all__ = ["AuditService"]
