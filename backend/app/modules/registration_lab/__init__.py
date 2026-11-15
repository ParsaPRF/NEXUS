"""Module C - Registration Lab.

Automates registration flows in applications the owner controls or is explicitly authorized to
test. The bundled demonstration application is the default target.

Boundaries that are architectural, not configurable:

* a target must be authorized **and** documented, and external targets must be allowlisted;
* a target answering 429 / CAPTCHA / access-denied stops the campaign - it is recorded as a
  terminal outcome, never retried around;
* registration has no proxy parameter at all, so the proxy pool cannot be used to disguise
  traffic (Decision D8);
* identities are synthetic addresses on domains this instance hosts.

Importing this package registers the campaign job handler.
"""

from app.modules.registration_lab import jobs as _jobs  # noqa: F401  (registers job handlers)

__all__: list[str] = []
