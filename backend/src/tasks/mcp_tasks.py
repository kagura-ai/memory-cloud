"""MCP background tasks.

Implements:
- MCP session cleanup (periodic)
"""

import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mcp_server.session import get_session_manager
from utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# BUG FIX #83-11: MCP session cleanup scheduler
# ============================================================================
# Problem: MCPSessionManager.cleanup_inactive_sessions() was implemented
#          but never called. This causes memory leak in long-running servers
#          as sessions accumulate indefinitely.
#
# Solution: Add periodic APScheduler task to cleanup inactive sessions.
#
# Configuration:
#          - MCP_SESSION_TIMEOUT_SECONDS: Inactivity timeout (default: 1 hour)
#          - MCP_SESSION_CLEANUP_INTERVAL: Cleanup interval (default: 10 min)
#
# Impact: Prevents memory leak in production deployments.
# ============================================================================


async def cleanup_mcp_sessions_task():
    """Cleanup inactive MCP sessions to prevent memory leak."""
    timeout = int(os.getenv("MCP_SESSION_TIMEOUT_SECONDS", "3600"))  # 1 hour

    try:
        manager = get_session_manager()
        await manager.cleanup_inactive_sessions(timeout_seconds=timeout)

        logger.info("mcp_session_cleanup_completed", timeout=timeout)

    except Exception as e:
        logger.error(f"mcp_session_cleanup_failed: {e}", exc_info=True)


def schedule_mcp_tasks(scheduler: AsyncIOScheduler) -> None:
    """Schedule MCP-related background tasks.

    Args:
        scheduler: APScheduler instance
    """
    # MCP Session Cleanup: Every 10 minutes (configurable)
    interval = int(os.getenv("MCP_SESSION_CLEANUP_INTERVAL", "600"))  # 10 minutes

    scheduler.add_job(
        cleanup_mcp_sessions_task,
        trigger=IntervalTrigger(seconds=interval),
        id="mcp_session_cleanup",
        name="MCP Session Cleanup",
        replace_existing=True,
    )

    logger.info("scheduled_mcp_session_cleanup_task", interval=interval)
