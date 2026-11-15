"""Task Manager service: create, observe, cancel and report on background jobs."""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger, redact
from app.models.email_models import Inbox
from app.models.enums import CampaignStatus, InboxStatus, JobStatus, ProxyStatus
from app.models.job_models import BackgroundJob, JobLog
from app.models.proxy_models import Proxy
from app.models.registration_models import Campaign
from app.services.audit_service import AuditService

logger = get_logger(__name__)

#: Datasets that the analytics export knows how to produce.
EXPORT_DATASETS = ("jobs", "campaigns")

#: Cap on an export, so "export everything" cannot produce a giant response.
MAX_EXPORT_ROWS = 10_000

#: Terminal job states.
TERMINAL = {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}


class JobService:
    """Use cases for background jobs and analytics."""

    def __init__(self, session: Any, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._audit = AuditService(session, enabled=settings.audit_log_enabled)

    # ------------------------------------------------------------------ create

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        request: Any | None = None,
    ) -> tuple[BackgroundJob, str, bool]:
        """Create the job row and schedule it.

        ``idempotency_key`` makes the call safe to retry: an existing row with the same key is
        returned and nothing is enqueued a second time. That is the mechanism that stops a
        retried request from checking every proxy twice or running a campaign twice.

        Returns:
            ``(job, queue_job_id, created)`` - ``created`` is false when an existing job was
            reused.

        Raises:
            NotFoundError: no handler is registered for ``job_type`` (the queue raises).
        """
        safe_payload = redact(payload or {})

        if idempotency_key:
            existing = await self._find_by_key(idempotency_key)
            if existing is not None:
                return existing, existing.queue_job_id or "", False

        job = BackgroundJob(
            public_id=str(uuid.uuid4()),
            job_type=job_type,
            status=JobStatus.QUEUED.value,
            payload_json=safe_payload,
            idempotency_key=idempotency_key,
            progress_total=int(safe_payload.get("progress_total", 0) or 0),
            queued_at=datetime.now(UTC),
        )
        self._session.add(job)
        await self._session.flush()

        from app.jobs.queue import get_queue

        queue_job_id = await get_queue(self._settings).enqueue(
            job_type, {**safe_payload, "job_db_id": job.id}
        )
        job.queue_job_id = queue_job_id

        await self._audit.record(
            "job.enqueue",
            entity_type="job",
            entity_id=job.public_id,
            detail={"job_type": job_type, "idempotency_key": idempotency_key},
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
        await self._session.commit()
        return job, queue_job_id, True

    async def _find_by_key(self, key: str) -> BackgroundJob | None:
        result = await self._session.execute(
            select(BackgroundJob).where(BackgroundJob.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ read

    async def get(self, public_id: str) -> BackgroundJob:
        """Return one job by its public id.

        Raises:
            NotFoundError: no job has that public id.
        """
        result = await self._session.execute(
            select(BackgroundJob).where(BackgroundJob.public_id == public_id)
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise NotFoundError(
                f"Job {public_id!r} was not found.", details={"public_id": public_id}
            )
        return job

    async def logs(self, public_id: str, *, limit: int = 200) -> list[JobLog]:
        """Return a job's log lines, oldest first."""
        job = await self.get(public_id)
        result = await self._session.execute(
            select(JobLog)
            .where(JobLog.job_id == job.id)
            .order_by(JobLog.created_at, JobLog.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_jobs(
        self,
        *,
        statuses: list[str] | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BackgroundJob], int]:
        """Return a filtered page of jobs, newest first."""
        statement = select(BackgroundJob)
        if statuses:
            statement = statement.where(BackgroundJob.status.in_(statuses))
        if job_type:
            statement = statement.where(BackgroundJob.job_type == job_type)
        total = (
            await self._session.execute(select(func.count()).select_from(statement.subquery()))
        ).scalar_one()
        rows = (
            (
                await self._session.execute(
                    statement.order_by(BackgroundJob.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    # ------------------------------------------------------------------ cancel

    async def request_cancel(self, public_id: str, *, request: Any | None = None) -> BackgroundJob:
        """Ask a job to stop.

        Two things happen: the flag is set (which the running job reads through its tracker), and
        the queue is asked to cancel (which covers a job that has not started yet).

        Raises:
            ConflictError: the job already finished.
        """
        job = await self.get(public_id)
        if job.status in TERMINAL:
            raise ConflictError(f"Job is already {job.status}.")

        job.cancel_requested = True
        await self._session.flush()

        # Commit BEFORE waiting on the queue: the running job reads this flag through its own
        # session, so an uncommitted flag is invisible to it. Without this commit the job kept
        # running until the queue forced a cancellation, and the row stayed 'running'.
        await self._audit.record(
            "job.cancel",
            entity_type="job",
            entity_id=job.public_id,
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
        await self._session.commit()

        if job.queue_job_id:
            from app.jobs.queue import get_queue

            try:
                await get_queue(self._settings).cancel(job.queue_job_id)
            except Exception:  # noqa: BLE001 - the flag still applies; log and continue
                logger.warning(
                    "Queue refused cancellation for %s",
                    job.queue_job_id,
                    extra={"module_name": "task_manager"},
                )

        # A queued job the queue never started is marked cancelled directly.
        if job.status == JobStatus.QUEUED.value and not job.queue_job_id:
            job.status = JobStatus.CANCELLED.value
            job.finished_at = datetime.now(UTC)
            await self._session.commit()
        return job

    # ------------------------------------------------------------------ analytics

    async def summary(self) -> dict[str, Any]:
        """Counts for the dashboard, plus the most recent audit activity."""
        from app.models.core_models import AuditLog

        jobs = await self._count_by(BackgroundJob, BackgroundJob.status)
        proxies = await self._count_by(Proxy, Proxy.status)
        inboxes = await self._count_by(Inbox, Inbox.status)
        campaigns = await self._count_by(Campaign, Campaign.status)

        recent = (
            (await self._session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(10)))
            .scalars()
            .all()
        )

        return {
            "jobs": jobs,
            "proxies": proxies,
            "inboxes": inboxes,
            "campaigns": campaigns,
            "total_jobs": sum(jobs.values()),
            "total_proxies": sum(proxies.values()),
            "total_inboxes": sum(inboxes.values()),
            "total_campaigns": sum(campaigns.values()),
            "active_inboxes": inboxes.get(InboxStatus.ACTIVE.value, 0),
            "healthy_proxies": proxies.get(ProxyStatus.HEALTHY.value, 0),
            "running_campaigns": campaigns.get(CampaignStatus.RUNNING.value, 0),
            "running_jobs": jobs.get(JobStatus.RUNNING.value, 0),
            "completed_jobs": jobs.get(JobStatus.COMPLETED.value, 0),
            "failed_jobs": jobs.get(JobStatus.FAILED.value, 0),
            "recent_activity": [
                {
                    "action": row.action,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "outcome": row.outcome,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in recent
            ],
        }

    async def _count_by(self, model: Any, column: Any) -> dict[str, int]:
        result = await self._session.execute(select(column, func.count()).group_by(column))
        return {str(key): int(count) for key, count in result.all() if key is not None}

    async def export(self, dataset: str, fmt: str) -> tuple[str, str]:
        """Export jobs or campaigns as CSV or JSON.

        Raises:
            ValidationError: unknown dataset or format.
        """
        resolved = (fmt or "csv").lower()
        if resolved not in {"csv", "json"}:
            raise ValidationError(
                f"Unsupported export format {fmt!r}.", details={"allowed": ["csv", "json"]}
            )
        if dataset not in EXPORT_DATASETS:
            raise ValidationError(
                f"Unsupported dataset {dataset!r}.", details={"allowed": list(EXPORT_DATASETS)}
            )

        if dataset == "jobs":
            rows = (
                (
                    await self._session.execute(
                        select(BackgroundJob)
                        .order_by(BackgroundJob.id.desc())
                        .limit(MAX_EXPORT_ROWS)
                    )
                )
                .scalars()
                .all()
            )
            records = [
                {
                    "public_id": row.public_id,
                    "job_type": row.job_type,
                    "status": row.status,
                    "progress_current": row.progress_current,
                    "progress_total": row.progress_total,
                    "attempts": row.attempts,
                    "duration_ms": row.duration_ms if row.duration_ms is not None else "",
                    "queued_at": row.queued_at.isoformat() if row.queued_at else "",
                    "finished_at": row.finished_at.isoformat() if row.finished_at else "",
                    "error_message": row.error_message or "",
                }
                for row in rows
            ]
        else:
            rows = (
                (
                    await self._session.execute(
                        select(Campaign).order_by(Campaign.id.desc()).limit(MAX_EXPORT_ROWS)
                    )
                )
                .scalars()
                .all()
            )
            records = [
                {
                    "public_id": row.public_id,
                    "name": row.name,
                    "status": row.status,
                    "total_tests": row.total_tests,
                    "completed_tests": row.completed_tests,
                    "succeeded_tests": row.succeeded_tests,
                    "failed_tests": row.failed_tests,
                    "started_at": row.started_at.isoformat() if row.started_at else "",
                    "finished_at": row.finished_at.isoformat() if row.finished_at else "",
                    "stop_reason": row.stop_reason or "",
                }
                for row in rows
            ]

        if resolved == "json":
            return json.dumps(records, indent=2, ensure_ascii=False), "application/json"

        buffer = io.StringIO()
        if records:
            writer = csv.DictWriter(buffer, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
        return buffer.getvalue(), "text/csv"


__all__ = ["EXPORT_DATASETS", "JobService", "MAX_EXPORT_ROWS", "TERMINAL"]
