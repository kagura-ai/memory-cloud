"""Background tasks for Kagura Memory Cloud.

Implements scheduled tasks for:
- Neural Memory weight decay (hourly)
- Memory consolidation (daily)
- Cleanup of old memories (daily)
- MCP session cleanup (every 10 minutes)
- Auto-hide expired credentials (hourly) - Migration 034
- Resource indexer (every 5 minutes) - Issue #238
- Semantic edge dead-endpoint sweep (monthly) — Issue #722
"""

from .bm25_drift_tasks import schedule_bm25_drift_tasks
from .credentials_tasks import schedule_credentials_tasks
from .embedding_tasks import schedule_embedding_tasks
from .erasure_tasks import schedule_erasure_tasks
from .file_tasks import schedule_file_tasks
from .mcp_tasks import schedule_mcp_tasks
from .neural_tasks import schedule_neural_tasks
from .resource_indexer_job import schedule_resource_indexer_jobs
from .scheduler import get_scheduler, shutdown_scheduler, start_scheduler
from .semantic_edge_reverify import schedule_semantic_edge_reverify_tasks
from .sleep_tasks import schedule_sleep_tasks

__all__ = [
    "get_scheduler",
    "start_scheduler",
    "shutdown_scheduler",
    "schedule_neural_tasks",
    "schedule_mcp_tasks",
    "schedule_credentials_tasks",
    "schedule_embedding_tasks",
    "schedule_erasure_tasks",
    "schedule_resource_indexer_jobs",
    "schedule_sleep_tasks",
    "schedule_bm25_drift_tasks",
    "schedule_file_tasks",
    "schedule_semantic_edge_reverify_tasks",
]
