"""MCP Session Management.

Manages multiple MCP sessions for concurrent clients.
Based on v4.4.0 MCPSessionManager implementation.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from mcp.server import Server

from utils.datetime import utcnow

logger = logging.getLogger(__name__)


@dataclass
class MCPSession:
    """Represents an active MCP session for a specific client.

    Issue #146: Now includes workspace_id for workspace-scoped API keys.
    Issue #245: Removed context_id (now required in tool args, not session).
    Issue #248: Removed SSE transport (deprecated, only Streamable HTTP supported).

    Each session stores the MCP server instance and user/workspace info.
    Streamable HTTP is stateless but needs session to maintain server instance.
    """

    session_id: str
    user_id: str
    workspace_id: UUID | None  # Issue #146: Workspace ID (None for personal)
    server: Server
    created_at: datetime = field(default_factory=utcnow)
    last_active_at: datetime = field(default_factory=utcnow)


class MCPSessionManager:
    """Manages multiple MCP sessions for concurrent clients.

    Issue #248: SSE transport removed (deprecated).
    Now only supports Streamable HTTP transport.

    Creates isolated sessions for each client, enabling true multi-client support.
    Each session maintains its own MCP server instance.
    """

    def __init__(self):
        """Initialize session manager."""
        self._sessions: dict[str, MCPSession] = {}
        self._lock = asyncio.Lock()

    def generate_session_id(self) -> str:
        """Generate cryptographically secure session ID.

        Uses UUID v4 for session identification per MCP Streamable HTTP spec.

        Returns:
            UUID v4 as string (e.g., "550e8400-e29b-41d4-a716-446655440000")
        """
        return str(uuid4())

    async def get_session(self, session_id: str) -> MCPSession | None:
        """Get existing session by ID with activity tracking.

        Issue #163: Thread-safe with lock to prevent race conditions when
        updating last_active_at. This prevents session timeout during active use.

        Args:
            session_id: Session ID to look up

        Returns:
            MCPSession if found, None otherwise
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.last_active_at = utcnow()
                logger.debug(
                    f"Session accessed: session_id={session_id}, "
                    f"user_id={session.user_id}, last_active={session.last_active_at}"
                )
            else:
                logger.debug(f"Session not found: session_id={session_id}")
            return session

    async def get_or_create_session(
        self,
        user_id: str,
        workspace_id: UUID | None = None,  # Workspace ID (Issue #146)
        session_id: str | None = None,
    ) -> MCPSession:
        """Get existing session or create new one.

        Issue #146: Now accepts workspace_id for workspace-scoped API keys.
        Issue #245: Removed context_id (now required in tool args, not session).

        Args:
            user_id: Authenticated user ID
            workspace_id: Workspace ID (None for personal)
            session_id: Optional session ID from client header

        Returns:
            MCPSession instance (new or existing)
        """
        async with self._lock:
            # Generate session ID if not provided
            if session_id is None:
                session_id = f"mcp-{uuid4().hex[:16]}"

            # Return existing session if found
            if session_id in self._sessions:
                session = self._sessions[session_id]

                # Issue #102: Security - validate user_id to prevent session hijacking
                if session.user_id != user_id:
                    logger.warning(
                        f"Session hijack attempt: session={session_id}, "
                        f"owner={session.user_id}, requester={user_id}"
                    )
                    raise PermissionError("Session belongs to a different user")

                # Issue #146: Security - validate workspace_id to prevent cross-workspace access
                if session.workspace_id != workspace_id:
                    logger.warning(
                        f"Session workspace mismatch: session={session_id}, "
                        f"stored={session.workspace_id}, requested={workspace_id}"
                    )
                    raise PermissionError("Session workspace mismatch")

                session.last_active_at = utcnow()
                logger.info(
                    f"MCP session reused: {session_id} (user={user_id}, workspace={workspace_id})"
                )
                return session

            # Create new session
            logger.info(
                f"MCP session creating: {session_id} (user={user_id}, workspace={workspace_id})"
            )

            # Create isolated server for this session
            from mcp_server.mcp_server import create_mcp_server

            server = create_mcp_server(
                user_id=user_id,
                workspace_id=workspace_id,  # Issue #146
            )

            session = MCPSession(
                session_id=session_id,
                user_id=user_id,
                workspace_id=workspace_id,  # Issue #146
                server=server,
            )

            self._sessions[session_id] = session
            logger.info(f"MCP session created: {session_id} (total={len(self._sessions)})")
            return session

    # Issue #245: update_session_context() removed (switch_context tool deleted)

    async def remove_session(self, session_id: str):
        """Remove and cleanup session with timeout.

        Args:
            session_id: Session ID to remove
        """
        # Issue #102: Lock only for pop (instant operation)
        async with self._lock:
            if session_id in self._sessions:
                self._sessions.pop(session_id)
                logger.info(f"MCP session removing: {session_id} (remaining={len(self._sessions)})")

        # Session removed successfully (no cleanup needed for Streamable HTTP)
        logger.debug(f"MCP session removed: {session_id}")

    async def cleanup_inactive_sessions(self, timeout_seconds: int = 3600):
        """Remove sessions inactive for more than timeout_seconds.

        Args:
            timeout_seconds: Inactivity timeout (default: 1 hour)
        """
        now = utcnow()

        # Issue #102: Lock only for listing (avoid deadlock)
        async with self._lock:
            to_remove = [
                session_id
                for session_id, session in self._sessions.items()
                if (now - session.last_active_at).total_seconds() > timeout_seconds
            ]

        # Delete outside lock (remove_session() acquires lock internally)
        for session_id in to_remove:
            logger.info(f"mcp_session_cleanup_inactive: {session_id}")
            await self.remove_session(session_id)


# Global session manager
_session_manager = MCPSessionManager()


def get_session_manager() -> MCPSessionManager:
    """Get global session manager instance.

    Returns:
        MCPSessionManager singleton
    """
    return _session_manager
