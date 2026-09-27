"""MCP Session Management.

Manages multiple MCP sessions for concurrent clients.
Based on v4.4.0 MCPSessionManager implementation.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from mcp.server import Server

from utils.datetime import utcnow

logger = logging.getLogger(__name__)

# How ``MCPSessionManager.get_owned_session`` found a session id (#1686).
# ``terminated`` (#1740): not held, and ended by ``DELETE`` within the idle
# timeout — the one unknown id that must never be re-adopted.
SessionLookup = Literal["owned", "foreign", "missing", "terminated"]

# The shape of every id this server mints (``mcp-`` + 16 lowercase hex). Only
# an unknown id of this shape is re-adopted (#1686, #1740); a client-invented
# one was never issued, and the spec answers it 404.
_SERVER_MINTED_SESSION_ID = re.compile(r"mcp-[0-9a-f]{16}")

# A tombstone lasts the idle timeout, and the map is bounded so a client
# looping initialize → DELETE cannot grow it without limit. When full,
# the oldest tombstone goes first: its id falls back to the re-adoption rule.
DEFAULT_MAX_TOMBSTONES = 10_000


def is_server_minted_session_id(session_id: str) -> bool:
    """Whether ``session_id`` has the shape of an id this server mints."""
    return _SERVER_MINTED_SESSION_ID.fullmatch(session_id) is not None


def session_timeout_seconds() -> int:
    """The MCP session idle timeout (``MCP_SESSION_TIMEOUT_SECONDS``, default 1 hour).

    Shared by the idle cleanup task and the ``DELETE`` tombstones (#1740).
    """
    return int(os.getenv("MCP_SESSION_TIMEOUT_SECONDS", "3600"))


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
    # The legacy revision ``initialize`` negotiated (#1740): it decides whether
    # a JSON-RPC batch is accepted. ``None`` for a session re-adopted after a
    # restart, whose handshake this process never saw.
    protocol_version: str | None = None


class MCPSessionManager:
    """Manages multiple MCP sessions for concurrent clients.

    Issue #248: SSE transport removed (deprecated).
    Now only supports Streamable HTTP transport.

    Creates isolated sessions for each client, enabling true multi-client support.
    Each session maintains its own MCP server instance.
    """

    def __init__(
        self,
        *,
        tombstone_ttl_seconds: int | None = None,
        max_tombstones: int = DEFAULT_MAX_TOMBSTONES,
    ):
        """Initialize session manager.

        Args:
            tombstone_ttl_seconds: How long an id ended by ``DELETE`` stays
                refused; the idle timeout when ``None``.
            max_tombstones: Bound on the tombstone map.
        """
        self._sessions: dict[str, MCPSession] = {}
        # #1740: ids ended by ``DELETE`` → when their tombstone expires, in
        # insertion (hence expiry) order.
        self._tombstones: dict[str, datetime] = {}
        self._tombstone_ttl_seconds = tombstone_ttl_seconds
        self._max_tombstones = max_tombstones
        self._lock = asyncio.Lock()

    def generate_session_id(self) -> str:
        """Generate a cryptographically secure session ID.

        ``mcp-`` + 16 hex characters (64 random bits from UUID v4) — the shape
        ``is_server_minted_session_id`` recognises.
        """
        return f"mcp-{uuid4().hex[:16]}"

    def _is_tombstoned(self, session_id: str) -> bool:
        """Whether ``session_id`` was ended by ``DELETE`` and is still refused.

        Caller holds ``self._lock``. An expired tombstone is dropped here.
        """
        expires_at = self._tombstones.get(session_id)
        if expires_at is None:
            return False
        if expires_at <= utcnow():
            del self._tombstones[session_id]
            return False
        return True

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

    async def get_owned_session(
        self, session_id: str, user_id: str, workspace_id: UUID | None
    ) -> tuple[SessionLookup, MCPSession | None]:
        """Look up ``session_id`` for a caller, touching only the caller's own session.

        #1686: ownership is decided before activity is recorded. A session held
        by another user or workspace keeps its ``last_active_at``, so a
        rejected id still idles out (and can then be opened by its caller).

        Args:
            session_id: Session ID from the request
            user_id: The authenticated caller
            workspace_id: The caller's workspace for this request

        Returns:
            ``("owned", session)`` with ``last_active_at`` refreshed,
            ``("foreign", None)`` for another user's or workspace's session,
            ``("terminated", None)`` for an id ended by ``DELETE`` within the
            idle timeout, or ``("missing", None)`` when no session has that id.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return ("terminated" if self._is_tombstoned(session_id) else "missing"), None
            if session.user_id != user_id or session.workspace_id != workspace_id:
                logger.warning(
                    f"MCP session owner mismatch: session={session_id}, "
                    f"owner={session.user_id}, requester={user_id}, "
                    f"stored_workspace={session.workspace_id}, requested_workspace={workspace_id}"
                )
                return "foreign", None
            session.last_active_at = utcnow()
            return "owned", session

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

        Raises:
            PermissionError: The id belongs to another user or workspace, or
                was ended by ``DELETE`` within the idle timeout (#1740).
        """
        async with self._lock:
            # Generate session ID if not provided
            if session_id is None:
                session_id = self.generate_session_id()
            elif session_id not in self._sessions and self._is_tombstoned(session_id):
                # A DELETE landed between the caller's lookup and this call.
                raise PermissionError("Session was terminated")

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

    async def terminate_session(self, session_id: str) -> None:
        """End a session at the client's request (``DELETE /mcp``) and tombstone its id.

        #1740: the spec answers a request naming a terminated session 404, so
        the id is refused — to every caller, never re-adopted — until the idle
        timeout, after which it is indistinguishable from one lost to the idle
        cleanup. Tombstones live in process memory, like the sessions.
        """
        ttl = self._tombstone_ttl_seconds
        if ttl is None:
            ttl = session_timeout_seconds()
        async with self._lock:
            self._sessions.pop(session_id, None)
            self._tombstones.pop(session_id, None)  # re-inserted last: newest
            while self._tombstones and len(self._tombstones) >= self._max_tombstones:
                del self._tombstones[next(iter(self._tombstones))]
            self._tombstones[session_id] = utcnow() + timedelta(seconds=ttl)
            logger.info(
                f"MCP session terminated: {session_id} (remaining={len(self._sessions)}, "
                f"tombstones={len(self._tombstones)})"
            )

    async def cleanup_inactive_sessions(self, timeout_seconds: int = 3600):
        """Remove sessions inactive for more than timeout_seconds.

        Args:
            timeout_seconds: Inactivity timeout (default: 1 hour)
        """
        now = utcnow()

        # Issue #102: Lock only for listing (avoid deadlock)
        async with self._lock:
            # #1740: expired tombstones go with the idle sessions. Insertion
            # order is expiry order (one TTL for all), so stop at the first
            # live one.
            while self._tombstones:
                oldest = next(iter(self._tombstones))
                if self._tombstones[oldest] > now:
                    break
                del self._tombstones[oldest]
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
