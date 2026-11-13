"""Background tasks for Kagura Memory Cloud.

Implements scheduled tasks for:
- Neural Memory weight decay (hourly)
- Memory consolidation (daily)
- Cleanup of old memories (daily)
- Auto-hide expired credentials (hourly) - Migration 034
- Resource indexer (every 5 minutes) - Issue #238
- Semantic edge dead-endpoint sweep (monthly) — Issue #722
- Expired device code cleanup (hourly) — Issue #1656
- Security notice digests (every minute) — Issue #1752
- Used/expired email action token cleanup (hourly) — Issue #1738
- Capacity-over purge sweep (daily 04:00 UTC) — Issue #1969
- Pending referral grant sweep (every 5 minutes) — Issue #2563
- Public MCP usage event retention (daily 04:50 UTC) — Issue #2678
- Expired operation history purge (daily 05:10 UTC) — Issue #2739

Every job above is leader-only: one process of the deployment runs the
scheduler (``scheduler_leader``, Issue #1980) and adds them with
:func:`register_scheduled_jobs`. They work on Postgres / Qdrant / Redis state,
so where they run does not matter.

Every job's runs are recorded in ``scheduled_job_status`` (Issue #2768) by
one listener, :func:`tasks.job_runs.attach_job_run_recorder`, attached as the
LAST step of :func:`register_scheduled_jobs`, so a job added here is covered
without further work. What a job returns is published to operators by the
admin operations status route: return a mapping of integer counts under
code-literal snake_case keys (never data: no ids, names or messages), or
:func:`tasks.job_runs.job_failed` from a top-level ``except`` that handles the
failure itself; a job that raises is recorded as failed with its exception
class name.

MCP session cleanup (every 10 minutes) is NOT a scheduler job: it cleans the
process-local session table, so every worker runs its own loop
(:func:`tasks.mcp_tasks.start_local_session_cleanup`, started from the app
lifespan).
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .bm25_drift_tasks import schedule_bm25_drift_tasks
from .capacity_purge_tasks import schedule_capacity_purge_tasks
from .credentials_tasks import schedule_credentials_tasks
from .device_code_tasks import schedule_device_code_tasks
from .email_action_token_tasks import schedule_email_action_token_tasks
from .embedding_tasks import schedule_embedding_tasks
from .erasure_tasks import schedule_erasure_tasks
from .file_tasks import schedule_file_tasks
from .job_runs import attach_job_run_recorder
from .known_device_tasks import schedule_known_device_tasks
from .neural_tasks import schedule_neural_tasks
from .operation_history_tasks import schedule_operation_history_tasks
from .public_mcp_usage_tasks import schedule_public_mcp_usage_tasks
from .referral_tasks import schedule_referral_tasks
from .resource_indexer_job import schedule_resource_indexer_jobs
from .scheduler import get_scheduler, shutdown_scheduler, start_scheduler
from .security_notification_tasks import schedule_security_notification_tasks
from .semantic_edge_reverify import schedule_semantic_edge_reverify_tasks
from .sleep_tasks import schedule_sleep_tasks


def register_scheduled_jobs(scheduler: AsyncIOScheduler) -> None:
    """Add every background cron job to ``scheduler``.

    Called by the scheduler leader (Issue #1980) each time this process takes
    the leader lock — at startup or on takeover — before the scheduler starts.

    Args:
        scheduler: The (not yet started) process scheduler.
    """
    schedule_neural_tasks(scheduler)
    schedule_credentials_tasks(scheduler)
    schedule_device_code_tasks(scheduler)
    schedule_security_notification_tasks(scheduler)
    schedule_email_action_token_tasks(scheduler)
    schedule_known_device_tasks(scheduler)
    schedule_embedding_tasks(scheduler)
    schedule_resource_indexer_jobs(scheduler)
    schedule_sleep_tasks(scheduler)
    schedule_bm25_drift_tasks(scheduler)
    schedule_erasure_tasks(scheduler)
    schedule_file_tasks(scheduler)
    schedule_capacity_purge_tasks(scheduler)
    schedule_semantic_edge_reverify_tasks(scheduler)
    schedule_referral_tasks(scheduler)
    schedule_public_mcp_usage_tasks(scheduler)
    schedule_operation_history_tasks(scheduler)
    # Keep last: the recorder snapshots every job registered above (#2768).
    attach_job_run_recorder(scheduler)


__all__ = [
    "register_scheduled_jobs",
    "attach_job_run_recorder",
    "get_scheduler",
    "start_scheduler",
    "shutdown_scheduler",
    "schedule_neural_tasks",
    "schedule_credentials_tasks",
    "schedule_device_code_tasks",
    "schedule_security_notification_tasks",
    "schedule_email_action_token_tasks",
    "schedule_known_device_tasks",
    "schedule_embedding_tasks",
    "schedule_erasure_tasks",
    "schedule_resource_indexer_jobs",
    "schedule_sleep_tasks",
    "schedule_bm25_drift_tasks",
    "schedule_file_tasks",
    "schedule_semantic_edge_reverify_tasks",
    "schedule_capacity_purge_tasks",
    "schedule_referral_tasks",
    "schedule_public_mcp_usage_tasks",
    "schedule_operation_history_tasks",
]
