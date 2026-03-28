"""Background tasks for Kagura Memory Cloud.

Implements scheduled tasks for:
- Neural Memory weight decay (hourly)
- Memory consolidation (daily)
- Cleanup of old memories (daily)
- MCP session cleanup (every 10 minutes)
- Auto-hide expired credentials (hourly) - Migration 034
- Resource indexer (every 5 minutes) - Issue #238
"""

from .credentials_tasks import schedule_credentials_tasks
from .mcp_tasks import schedule_mcp_tasks
from .neural_tasks import schedule_neural_tasks
from .resource_indexer_job import schedule_resource_indexer_jobs
from .scheduler import get_scheduler, shutdown_scheduler, start_scheduler

__all__ = [
    "get_scheduler",
    "start_scheduler",
    "shutdown_scheduler",
    "schedule_neural_tasks",
    "schedule_mcp_tasks",
    "schedule_credentials_tasks",
    "schedule_resource_indexer_jobs",
]
