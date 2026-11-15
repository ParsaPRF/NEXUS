"""Module A - Proxy Manager.

Manages network proxies that the **user owns, operates, or is authorized to use**: importing
them from configured sources, normalizing them, checking their health against an endpoint the
operator owns, and exporting the results.

This module is deliberately **not connected to the Registration Lab**. There is no proxy
parameter anywhere in the registration code and no join table between proxies and campaigns, so
registration traffic cannot be spread across the proxy pool to evade a target's abuse detection
(docs/SECURITY.md 3, Decision D8).

Importing this package registers the batch-check job handler, which is why ``app.api.router``
imports it: the handler must exist before a client can enqueue that job type.
"""

from app.modules.proxy_manager import jobs as _jobs  # noqa: F401  (registers job handlers)

__all__: list[str] = []
