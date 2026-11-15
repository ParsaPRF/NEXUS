"""Proxy Manager service - the use cases.

This is where business rules live:

* an import de-duplicates against what is already stored, and **refuses** a credential-bearing
  proxy when no encryption key is configured rather than storing the password in plain text;
* a check updates both current state (``proxies``) and history (``proxy_checks``), and applies
  the expiry policy after repeated failures;
* every state-changing action writes an audit record;
* all limits (batch size, import size, concurrency) are enforced here, not only in the router,
  so a background job cannot bypass them by calling the service directly.

The service returns module schemas rather than a second set of DTOs. The schemas *are* this
module's contract with the outside world, and duplicating every field into a parallel dataclass
would create two things to keep in sync for no benefit.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import CredentialCipher
from app.models.enums import AuditOutcome, ProxyStatus
from app.models.proxy_models import Proxy, ProxyCheck, ProxySource
from app.modules.proxy_manager.checker import CheckOutcome, ProxyHealthChecker
from app.modules.proxy_manager.importers import (
    ImportOutcome,
    RejectedEntry,
    detect_format,
    import_content,
)
from app.modules.proxy_manager.repository import ProxyQuery, ProxyRepository
from app.modules.proxy_manager.schemas import (
    EXPORT_FIELDS,
    ProxyBatchCheckResponse,
    ProxyCheckResponse,
    ProxyImportResponse,
    ProxyRead,
    ProxyStatsResponse,
    RejectedEntryRead,
    proxy_to_export_row,
)
from app.services.audit_service import AuditService

logger = get_logger(__name__)

#: How many rejection samples are echoed back. Enough to diagnose a bad file, not enough to
#: become a bulk data channel.
MAX_REJECTED_SAMPLES = 10

#: Ceiling on a single export so "export everything" cannot generate an enormous response.
MAX_EXPORT_ROWS = 10_000


class ProxyService:
    """Use cases for importing, listing, checking and exporting proxies."""

    def __init__(
        self,
        session,
        settings: Settings,
        *,
        cipher: CredentialCipher | None = None,
        checker: ProxyHealthChecker | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._repo = ProxyRepository(session)
        self._cipher = cipher
        self._checker = checker or ProxyHealthChecker(settings, cipher)
        self._audit = AuditService(session, enabled=settings.audit_log_enabled)

    # ------------------------------------------------------------------ import

    async def import_list(
        self,
        content: str,
        *,
        fmt: str = "auto",
        name: str = "Untitled import",
        kind: str = "manual",
        location: str | None = None,
        request=None,
    ) -> ProxyImportResponse:
        """Parse and store a proxy list.

        Raises:
            ImportFileError: the content is empty, binary, oversized, or the format is unknown.
        """
        outcome: ImportOutcome = import_content(
            content, fmt, max_bytes=self._settings.proxy.max_import_bytes
        )
        detected = self._detect_format_name(content, fmt)

        existing = await self._repo.existing_identity_keys()
        source = await self._repo.add_source(
            ProxySource(
                name=name,
                kind=kind,
                location=location,
                imported_at=datetime.now(UTC),
            )
        )

        stored = 0
        duplicates = outcome.duplicates
        credentials_refused = 0

        for parsed in outcome.accepted:
            if parsed.identity_key in existing:
                duplicates += 1
                continue

            enc_password: str | None = None
            if parsed.has_credentials:
                # A proxy password must be replayed, so it cannot be hashed. It is encrypted with
                # a key from the environment - and if there is no key, the entry is refused. We
                # never fall back to plain text (docs/SECURITY.md T5).
                if not self._settings.proxy_credential_key:
                    credentials_refused += 1
                    continue
                cipher = self._cipher or CredentialCipher(self._settings.proxy_credential_key)
                enc_password = cipher.encrypt(parsed.password or "")
                self._cipher = cipher

            await self._repo.add_proxy(
                Proxy(
                    source_id=source.id,
                    protocol=parsed.protocol,
                    host=parsed.host,
                    port=parsed.port,
                    username=parsed.username,
                    password_encrypted=enc_password,
                    status=ProxyStatus.UNKNOWN.value,
                )
            )
            existing.add(parsed.identity_key)
            stored += 1

        source.entry_count = outcome.entries_seen
        source.accepted_count = stored
        # Everything not stored is "rejected" from the database's point of view: that covers
        # malformed entries, duplicates and refused credentials.
        source.rejected_count = max(0, outcome.entries_seen - stored)

        await self._audit.record(
            "proxy.import",
            entity_type="proxy_source",
            entity_id=source.id,
            detail={
                "source_name": name,
                "format": detected,
                "entries_seen": outcome.entries_seen,
                "stored": stored,
                "duplicates": duplicates,
                "rejected": len(outcome.rejected),
                "credentials_refused": credentials_refused,
            },
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
        await self._session.commit()

        logger.info(
            "Imported proxies: %d stored, %d duplicates, %d rejected, %d credentials refused",
            stored,
            duplicates,
            len(outcome.rejected),
            credentials_refused,
            extra={"module_name": "proxy_manager"},
        )

        return ProxyImportResponse(
            source_id=source.id,
            source_name=source.name,
            format_detected=detected,
            entries_seen=outcome.entries_seen,
            accepted=stored,
            duplicates=duplicates,
            rejected=len(outcome.rejected),
            rejected_samples=[_rejected_to_read(item) for item in outcome.rejected],
            credentials_refused=credentials_refused,
        )

    @staticmethod
    def _detect_format_name(content: str, fmt: str) -> str:
        """Report which parser actually ran, for the API response."""
        if fmt not in {"auto", "", None}:
            return fmt
        return detect_format(content)

    # ------------------------------------------------------------------ listing

    async def list_proxies(self, query: ProxyQuery) -> tuple[list[ProxyRead], int]:
        """Return a page of proxies as read schemas, plus the total count."""
        rows, total = await self._repo.list_proxies(query)
        return [ProxyRead.model_validate(row) for row in rows], total

    async def get_proxy(self, proxy_id: int) -> Proxy:
        """Return one proxy.

        Raises:
            NotFoundError: no proxy has that id.
        """
        proxy = await self._repo.get_proxy(proxy_id)
        if proxy is None:
            raise NotFoundError(f"Proxy {proxy_id} was not found.", details={"proxy_id": proxy_id})
        return proxy

    # ------------------------------------------------------------------ checks

    async def check_proxy(self, proxy_id: int, *, request=None) -> ProxyCheckResponse:
        """Check one proxy, persist the result and return it."""
        proxy = await self.get_proxy(proxy_id)
        outcome = await self._checker.check(proxy)
        await self._apply_outcome(proxy, outcome)

        await self._audit.record(
            "proxy.check",
            entity_type="proxy",
            entity_id=proxy.id,
            outcome=AuditOutcome.SUCCESS if outcome.ok else AuditOutcome.ERROR,
            detail={
                "ok": outcome.ok,
                "status": proxy.status,
                "latency_ms": outcome.latency_ms,
                "error_class": outcome.error_class,
            },
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
        await self._session.commit()
        return _outcome_to_response(proxy, outcome)

    async def check_by_ids(self, proxy_ids: list[int]) -> list[ProxyCheckResponse]:
        """Check several proxies, bounded by ``proxy.batch_limit``.

        Used by the batch job handler. Raises before doing any work when the request is too large,
        so an operator sees the limit rather than a half-finished batch.
        """
        if not proxy_ids:
            raise ValidationError("At least one proxy id is required.")
        if len(proxy_ids) > self._settings.proxy.batch_limit:
            raise ValidationError(
                f"A batch may check at most {self._settings.proxy.batch_limit} proxies.",
                details={"requested": len(proxy_ids), "limit": self._settings.proxy.batch_limit},
            )

        proxies = await self._repo.get_proxies(proxy_ids)
        if not proxies:
            return []

        results: list[ProxyCheckResponse] = []
        for proxy, outcome in await self._checker.check_many(proxies):
            await self._apply_outcome(proxy, outcome)
            results.append(_outcome_to_response(proxy, outcome))
        await self._session.commit()
        return results

    async def _apply_outcome(self, proxy: Proxy, outcome: CheckOutcome) -> None:
        """Update current state, append history, and apply the expiry policy.

        Expiry policy: a proxy that fails ``expire_after_failures`` checks in a row is marked
        ``expired`` rather than merely ``unhealthy``. That distinction matters operationally - an
        expired subscription or revoked credential will not recover by itself, so the UI can stop
        offering it.
        """
        now = datetime.now(UTC)
        proxy.last_checked_at = now

        await self._repo.add_check(
            ProxyCheck(
                proxy_id=proxy.id,
                checked_at=now,
                ok=outcome.ok,
                latency_ms=outcome.latency_ms,
                http_status=outcome.http_status,
                error_class=outcome.error_class,
                error_message=outcome.error_message,
            )
        )

        if outcome.ok:
            proxy.status = ProxyStatus.HEALTHY.value
            proxy.latency_ms = outcome.latency_ms
            proxy.last_success_at = now
            proxy.success_count = (proxy.success_count or 0) + 1
            proxy.consecutive_failures = 0
            proxy.last_error = None
            return

        proxy.failure_count = (proxy.failure_count or 0) + 1
        proxy.consecutive_failures = (proxy.consecutive_failures or 0) + 1
        proxy.last_error = outcome.error_message
        if proxy.consecutive_failures >= self._settings.proxy.expire_after_failures:
            proxy.status = ProxyStatus.EXPIRED.value
        else:
            proxy.status = ProxyStatus.UNHEALTHY.value

    # ------------------------------------------------------------------ mutation

    async def set_enabled(self, proxy_id: int, *, enabled: bool, request=None) -> ProxyRead:
        """Enable or disable a proxy without deleting its history."""
        proxy = await self.get_proxy(proxy_id)
        proxy.is_enabled = enabled
        await self._audit.record(
            "proxy.update",
            entity_type="proxy",
            entity_id=proxy.id,
            detail={"is_enabled": enabled},
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
        await self._session.commit()
        return ProxyRead.model_validate(proxy)

    async def delete_proxy(self, proxy_id: int, *, request=None) -> None:
        """Delete a proxy and its check history."""
        proxy = await self.get_proxy(proxy_id)
        address = proxy.display_address
        await self._repo.delete_proxy(proxy)
        await self._audit.record(
            "proxy.delete",
            entity_type="proxy",
            entity_id=proxy_id,
            detail={"address": address},
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
        await self._session.commit()

    # ------------------------------------------------------------------ batch job

    async def resolve_batch_ids(self, *, status_filter: str | None, limit: int) -> list[int]:
        """Resolve a status filter into concrete proxy ids, capped by ``limit``.

        Public so the router does not have to reach into the repository; the limit is applied in
        SQL so it cannot be bypassed by a large table.
        """
        query = ProxyQuery(statuses=[status_filter] if status_filter else [], limit=limit)
        return await self._repo.ids_matching(query)

    async def request_batch_check(
        self, proxy_ids: list[int], *, request=None
    ) -> ProxyBatchCheckResponse:
        """Validate and enqueue a batch check.

        The ids are resolved *now* so the response can report how many proxies will be checked and
        a bad request fails immediately, rather than inside a worker where the caller cannot see it.
        """
        if not proxy_ids:
            raise ValidationError(
                "Provide proxy_ids or a status_filter selecting proxies to check."
            )
        if len(proxy_ids) > self._settings.proxy.batch_limit:
            raise ValidationError(
                f"A batch may check at most {self._settings.proxy.batch_limit} proxies.",
                details={"requested": len(proxy_ids), "limit": self._settings.proxy.batch_limit},
            )
        found = await self._repo.get_proxies(proxy_ids)
        if not found:
            raise NotFoundError("None of the requested proxies exist.")

        from app.jobs.queue import get_queue

        queue = get_queue(self._settings)
        job_id = await queue.enqueue(
            "proxy.check_batch",
            {"proxy_ids": [proxy.id for proxy in found]},
            progress_total=len(found),
        )
        await self._audit.record(
            "proxy.check_batch",
            entity_type="job",
            entity_id=job_id,
            detail={"proxy_count": len(found)},
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
        await self._session.commit()
        return ProxyBatchCheckResponse(
            job_id=job_id, job_type="proxy.check_batch", proxy_count=len(found)
        )

    # ------------------------------------------------------------------ stats

    async def stats(self) -> ProxyStatsResponse:
        """Return dashboard counts."""
        return ProxyStatsResponse(
            total=await self._repo.total_count(),
            by_status=await self._repo.status_counts(),
            healthy_with_latency=await self._repo.healthy_with_latency_count(),
            average_latency_ms=await self._repo.average_latency_ms(),
        )

    # ------------------------------------------------------------------ export

    async def export(self, fmt: str, query: ProxyQuery) -> tuple[str, str]:
        """Export proxies as CSV or JSON.

        Returns ``(content, media_type)``. Credentials are never included: an export is a file
        that gets copied, mailed around and committed by accident, so it carries no secrets.
        """
        resolved = (fmt or "csv").lower()
        if resolved not in {"csv", "json"}:
            raise ValidationError(
                f"Unsupported export format {fmt!r}.", details={"allowed": ["csv", "json"]}
            )
        if query.limit > MAX_EXPORT_ROWS:
            raise ValidationError(f"An export may contain at most {MAX_EXPORT_ROWS} rows.")

        rows, _ = await self._repo.list_proxies(query)
        records = [proxy_to_export_row(ProxyRead.model_validate(row)) for row in rows]

        if resolved == "json":
            payload = json.dumps(records, indent=2, ensure_ascii=False)
            return payload, "application/json"

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_FIELDS))
        writer.writeheader()
        writer.writerows(records)
        return buffer.getvalue(), "text/csv"


def _outcome_to_response(proxy: Proxy, outcome: CheckOutcome) -> ProxyCheckResponse:
    """Map a check outcome plus the updated proxy to the API schema."""
    return ProxyCheckResponse(
        proxy_id=proxy.id,
        ok=outcome.ok,
        latency_ms=outcome.latency_ms,
        http_status=outcome.http_status,
        error_class=outcome.error_class,
        error_message=outcome.error_message,
        status=proxy.status,
    )


def _rejected_to_read(entry: RejectedEntry) -> RejectedEntryRead:
    """Map an importer rejection to the API schema."""
    return RejectedEntryRead(
        line_number=entry.line_number, reason=entry.reason, excerpt=entry.excerpt
    )


#: Re-exported so the router and the job handler share the same names for the documented limits.
__all__ = [
    "MAX_EXPORT_ROWS",
    "MAX_REJECTED_SAMPLES",
    "ProxyService",
    "ProxyStatsResponse",
]
