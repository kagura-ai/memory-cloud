"""MCP background tasks.

Implements:
- MCP session cleanup (periodic, in every worker process)
"""

import asyncio
import os

from mcp_server.session import get_session_manager, session_timeout_seconds
from utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# BUG FIX #83-11: MCP session cleanup
# ============================================================================
# Problem: MCPSessionManager.cleanup_inactive_sessions() was implemented
#          but never called. This causes memory leak in long-running servers
#          as sessions accumulate indefinitely.
#
# Solution: Periodically cleanup inactive sessions.
#
# Per process, not on the scheduler (#1980): the session table (legacy
# ``initialize``-era sessions and the #1740 DELETE tombstones) lives in each
# worker's memory, while APScheduler jobs run only on the one scheduler leader
# process. So every worker runs its own asyncio cleanup loop, started from the
# app lifespan with :func:`start_local_session_cleanup`.
#
# Configuration:
#          - MCP_SESSION_TIMEOUT_SECONDS: Inactivity timeout (default: 1 hour)
#          - MCP_SESSION_CLEANUP_INTERVAL: Cleanup interval (default: 10 min)
#
# Impact: Prevents memory leak in production deployments.
# ============================================================================


async def cleanup_mcp_sessions_task():
    """Cleanup inactive MCP sessions to prevent memory leak."""
    timeout = session_timeout_seconds()  # 1 hour; also the DELETE tombstone TTL (#1740)

    try:
        manager = get_session_manager()
        await manager.cleanup_inactive_sessions(timeout_seconds=timeout)

        logger.info("mcp_session_cleanup_completed", timeout=timeout)

    except Exception as e:
        logger.error(f"mcp_session_cleanup_failed: {e}", exc_info=True)


async def _session_cleanup_loop(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await cleanup_mcp_sessions_task()
        except Exception:  # noqa: BLE001 — the loop must outlive one bad run
            logger.exception("mcp_session_cleanup_loop_error")


def start_local_session_cleanup(interval_seconds: float | None = None) -> asyncio.Task[None]:
    """Start this process's MCP session cleanup loop (#1980).

    Runs in every worker, unlike the APScheduler jobs, because each worker has
    its own session table. The caller must keep a strong reference to the
    returned task (the event loop holds tasks only weakly) and cancel it on
    shutdown.

    Args:
        interval_seconds: Seconds between cleanups; defaults to
            ``MCP_SESSION_CLEANUP_INTERVAL`` (600).

    Returns:
        The running cleanup task.
    """
    if interval_seconds is None:
        interval_seconds = int(os.getenv("MCP_SESSION_CLEANUP_INTERVAL", "600"))
    logger.info("mcp_session_cleanup_started", interval=interval_seconds, pid=os.getpid())
    return asyncio.create_task(
        _session_cleanup_loop(interval_seconds), name="mcp_session_cleanup_loop"
    )
