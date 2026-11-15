"""RQ worker entry point.

    uv run python -m app.workers.main

Requires ``NEXUS_JOBS__BACKEND=rq`` and a reachable Redis. In the compose stack the ``worker``
service runs this instead of the placeholder command it currently has.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


def main() -> None:
    """Start an RQ worker for the configured queue."""
    settings = get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    try:
        import redis  # noqa: PLC0415
        from rq import Queue, Worker  # noqa: PLC0415
    except ImportError:  # pragma: no cover - optional dependency
        msg = "RQ is not installed. Run: uv sync --extra queue"
        raise SystemExit(msg) from None

    connection = redis.Redis.from_url(settings.jobs.redis_url)
    queue = Queue(settings.jobs.queue_name, connection=connection)

    logger.info(
        "Starting RQ worker on queue %r (redis=%s)",
        settings.jobs.queue_name,
        settings.jobs.redis_url,
        extra={"module_name": "workers"},
    )
    worker = Worker([queue], connection=connection)
    worker.work(with_scheduler=False)


if __name__ == "__main__":  # pragma: no cover - process entry point
    logging.captureWarnings(True)
    main()
