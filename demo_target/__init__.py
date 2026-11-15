"""Bundled demonstration registration application (Phase 4).

NEXUS's default test target. It is a separate application: nothing here imports NEXUS's backend,
and it can only send mail through the mailer it is given.
"""

from demo_target.app import app, create_demo_app

__all__ = ["app", "create_demo_app"]