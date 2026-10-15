"""Background tasks for Kagura Memory Cloud.

Implements scheduled tasks for:
- Neural Memory weight decay (hourly)
- Memory consolidation (daily)
- Cleanup of old memories (daily)
- MCP session cleanup (every 10 minutes)
- Auto-hide expired credentials (hourly) - Migration 034
- Resource indexer (every 5 minutes) - Issue #238
- Semantic edge dead-endpoint sweep (monthly) — Issue #722
- Expired device code cleanup (hourly) — Issue #1656
- Security notice digests (every minute) — Issue #1752
- Used/expired email action token cleanup (hourly) — Issue #1738
- Capacity-over purge sweep (daily 04:00 UTC) — Issue #1969

Only one process of the deployment runs the scheduler (``scheduler_leader``,
Issue #1980); it adds the jobs with :func:`register_scheduled_jobs`.
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
from .known_device_tasks import schedule_known_device_tasks
from .mcp_tasks import schedule_mcp_tasks
from .neural_tasks import schedule_neural_tasks
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
    schedule_mcp_tasks(scheduler)
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


__all__ = [
    "register_scheduled_jobs",
    "get_scheduler",
    "start_scheduler",
    "shutdown_scheduler",
    "schedule_neural_tasks",
    "schedule_mcp_tasks",
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
]
