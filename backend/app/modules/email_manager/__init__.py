"""Module B - Email Manager.

Disposable test inboxes on a domain the owner controls, backed by Mailpit (or an in-memory
backend in tests). This module never creates a mailbox on a third-party provider: there is one
adapter for the local backend and no code path that speaks to Gmail, Outlook or anything else
(docs/SECURITY.md 3).

Importing this package registers its job handlers, which is why ``app.api.router`` imports it.
"""

from app.modules.email_manager import jobs as _jobs  # noqa: F401  (registers job handlers)

__all__: list[str] = []
