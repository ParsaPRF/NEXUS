"""SQLAlchemy models for every NEXUS module.

Import order in this module is significant and deliberate.

Alembic's autogenerate compares ``Base.metadata`` against the live database. A model that has
never been imported is simply not in the metadata, so the migration generator would consider its
table "not needed" and could even generate a ``DROP TABLE`` for it. Importing everything here,
and having :mod:`app.main` import this module, means the metadata is always complete before a
migration or a table-creation call runs.

The order below also resolves the two cross-module references:

* ``Inbox.campaign_id -> campaigns.id``  (email -> registration)
* ``Campaign.job_id    -> background_jobs.id`` (registration -> job)

Both are declared with string table names, so SQLAlchemy resolves them lazily when the mappers
are first configured - after every class above is defined.
"""

from app.database.base import Base
from app.models.core_models import AuditLog, User
from app.models.email_models import EmailDomain, Inbox, Message
from app.models.enums import (
    AuditOutcome,
    CampaignStatus,
    InboxStatus,
    JobLogLevel,
    JobStatus,
    ProxyProtocol,
    ProxySourceKind,
    ProxyStatus,
    ResultOutcome,
    TargetKind,
    TestStatus,
)
from app.models.job_models import BackgroundJob, JobLog
from app.models.proxy_models import Proxy, ProxyCheck, ProxySource
from app.models.registration_models import (
    Campaign,
    RegistrationResult,
    RegistrationTarget,
    RegistrationTest,
)

__all__ = [
    # base
    "Base",
    # enums
    "AuditOutcome",
    "CampaignStatus",
    "InboxStatus",
    "JobLogLevel",
    "JobStatus",
    "ProxyProtocol",
    "ProxySourceKind",
    "ProxyStatus",
    "ResultOutcome",
    "TargetKind",
    "TestStatus",
    # core
    "AuditLog",
    "User",
    # module A
    "Proxy",
    "ProxyCheck",
    "ProxySource",
    # module B
    "EmailDomain",
    "Inbox",
    "Message",
    # module C
    "Campaign",
    "RegistrationResult",
    "RegistrationTarget",
    "RegistrationTest",
    # module D
    "BackgroundJob",
    "JobLog",
]
