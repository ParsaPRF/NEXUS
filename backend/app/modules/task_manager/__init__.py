"""Module D - Task Manager & Analytics.

Owns the durable record of background work: ``background_jobs`` and ``job_logs``. Other modules
enqueue through :class:`~app.modules.task_manager.service.JobService` and register their handlers
through :func:`~app.modules.task_manager.tracker.register_tracked_handler`, so every job has a
row, progress, logs, a duration and a terminal state - which is what makes the Task Monitor and
the analytics reports possible.

The queue remains a dumb scheduler (see ``app.jobs.queue``); state lives in the database, so an
in-process run and an RQ run are described by the same rows.

This package intentionally imports nothing at module load: ``tracker`` is imported by the other
modules' job registrations, and an eager import here would create a cycle.
"""

__all__: list[str] = []
