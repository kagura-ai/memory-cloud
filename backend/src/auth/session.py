"""Redis-based session management for Web UI.

Issue #554 (Redis) + Issue #650 (Google OAuth2 Web Integration)

Provides secure session storage using Redis with singleton pattern.
"""

import json
import logging
import secrets
from typing import Any

from utils.datetime import utcnow

logger = logging.getLogger(__name__)

# Singleton Redis client cache (shared across all instances)
_redis_client_cache: dict[str, Any] = {}


class SessionManager:
    """Redis-based session manager for Web UI authentication.

    Manages user sessions with automatic expiration and secure session IDs.
    Uses singleton pattern for Redis connection pooling.

    Args:
        redis_url: Redis connection URL (e.g., redis://localhost:6379)
        session_ttl: Session lifetime in seconds (default: 7 days)

    Example:
        >>> manager = SessionManager(redis_url="redis://localhost:6379")
        >>>
        >>> # Create session after OAuth2 login
        >>> user_info = {"sub": "user_001", "email": "user@example.com"}
        >>> session_id = manager.create_session(user_info)
        >>>
        >>> # Get session (e.g., from cookie)
        >>> session = manager.get_session(session_id)
        >>> print(session["email"])  # user@example.com
        >>>
        >>> # Delete session on logout
        >>> manager.delete_session(session_id)

    Note:
        Multiple SessionManager instances with the same redis_url will share
        a single Redis client (and connection pool) for efficiency.
    """

    DEFAULT_SESSION_TTL = 7 * 24 * 3600  # 7 days

    def __init__(
        self,
        redis_url: str,
        session_ttl: int = DEFAULT_SESSION_TTL,
    ):
        """Initialize session manager.

        Args:
            redis_url: Redis connection URL
            session_ttl: Session lifetime in seconds (default: 7 days)

        Raises:
            ImportError: If redis package not installed
            ConnectionError: If unable to connect to Redis
        """
        self.redis_url = redis_url
        self.session_ttl = session_ttl

        # Get or create shared Redis client (singleton pattern)
        self._redis = self._get_or_create_redis_client(redis_url)

        logger.info(
            f"Initialized SessionManager (ttl={session_ttl}s, redis={redis_url.split('@')[-1]})"
        )

    @staticmethod
    def _get_or_create_redis_client(redis_url: str) -> Any:
        """Get or create Redis client (singleton pattern).

        Reuses existing client if already created for the same redis_url.
        This shares connection pool across all session manager instances.

        Args:
            redis_url: Redis connection URL

        Returns:
            Redis client instance (cached)

        Raises:
            ImportError: If redis package not installed
            ConnectionError: If unable to connect to Redis
        """
        global _redis_client_cache

        if redis_url not in _redis_client_cache:
            try:
                from redis import Redis

                logger.info(f"Creating new Redis client for sessions: {redis_url.split('@')[-1]}")

                client = Redis.from_url(
                    redis_url,
                    decode_responses=True,  # Auto-decode bytes to str
                    socket_connect_timeout=5,
                    socket_timeout=5,
                    retry_on_timeout=True,
                )

                # Test connection
                client.ping()

                _redis_client_cache[redis_url] = client
            except ImportError as e:
                raise ImportError(
                    "redis package not installed. Install with: pip install redis"
                ) from e
            except Exception as e:
                raise ConnectionError(f"Failed to connect to Redis: {e}") from e
        else:
            logger.debug(f"Reusing cached Redis client for {redis_url.split('@')[-1]}")

        return _redis_client_cache[redis_url]

    def create_session(self, user_info: dict[str, Any]) -> str:
        """Create new session for authenticated user.

        Args:
            user_info: User information from OAuth2 provider
                Required keys: "sub" (user ID)
                Optional keys: "email", "name", "picture", etc.

        Returns:
            Session ID (secure random token)

        Example:
            >>> user_info = {
            ...     "sub": "google_12345",
            ...     "email": "user@example.com",
            ...     "name": "John Doe"
            ... }
            >>> session_id = manager.create_session(user_info)
            >>> print(len(session_id))  # 43 (32 bytes URL-safe base64)
        """
        # Generate secure session ID
        session_id = secrets.token_urlsafe(32)

        # Session data
        session_data = {
            **user_info,
            "created_at": utcnow().isoformat(),
            "last_accessed": utcnow().isoformat(),
        }

        # Store in Redis with TTL
        try:
            self._redis.setex(
                f"session:{session_id}",
                self.session_ttl,
                json.dumps(session_data),
            )
            logger.info(f"Created session for user: {user_info.get('sub', 'unknown')}")
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

        return session_id

    def get_session(self, session_id: str, update_access: bool = True) -> dict[str, Any] | None:
        """Get session data.

        Args:
            session_id: Session ID
            update_access: Update last_accessed timestamp (default: True)

        Returns:
            Session data dict if session exists and is valid, None otherwise

        Example:
            >>> session = manager.get_session(session_id)
            >>> if session:
            ...     print(f"User: {session['email']}")
            ... else:
            ...     print("Session expired or invalid")
        """
        try:
            data = self._redis.get(f"session:{session_id}")
            if not data:
                return None

            session_data = json.loads(data)  # type: ignore[arg-type]

            # Update last accessed time and refresh TTL
            if update_access:
                session_data["last_accessed"] = utcnow().isoformat()
                self._redis.setex(
                    f"session:{session_id}",
                    self.session_ttl,
                    json.dumps(session_data),
                )

            return session_data

        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            return None

    def delete_session(self, session_id: str) -> bool:
        """Delete session (logout).

        Args:
            session_id: Session ID to delete

        Returns:
            True if session was deleted, False if session didn't exist

        Example:
            >>> manager.delete_session(session_id)
            >>> assert manager.get_session(session_id) is None
        """
        try:
            deleted = self._redis.delete(f"session:{session_id}")
            if deleted:
                logger.info(f"Deleted session: {session_id[:10]}...")
            return deleted > 0
        except Exception as e:
            logger.error(f"Failed to delete session: {e}")
            return False

    def update_session(self, session_id: str, updates: dict[str, Any]) -> bool:
        """Update session data.

        Args:
            session_id: Session ID
            updates: Dictionary of fields to update

        Returns:
            True if session was updated, False if session doesn't exist

        Example:
            >>> manager.update_session(session_id, {"preferences": {"theme": "dark"}})
        """
        try:
            # Get existing session
            session_data = self.get_session(session_id, update_access=False)
            if not session_data:
                return False

            # Update fields
            session_data.update(updates)
            session_data["updated_at"] = utcnow().isoformat()

            # Save back to Redis
            self._redis.setex(
                f"session:{session_id}",
                self.session_ttl,
                json.dumps(session_data),
            )

            logger.debug(f"Updated session: {session_id[:10]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to update session: {e}")
            return False

    def get_active_sessions_count(self) -> int:
        """Get count of active sessions.

        Returns:
            Number of active sessions

        Example:
            >>> count = manager.get_active_sessions_count()
            >>> print(f"Active users: {count}")
        """
        try:
            keys = self._redis.keys("session:*")
            return len(keys)
        except Exception as e:
            logger.error(f"Failed to count sessions: {e}")
            return -1

    def cleanup_expired_sessions(self) -> int:
        """Cleanup expired sessions (manual trigger).

        Returns:
            Number of sessions cleaned up

        Note:
            Redis automatically expires keys based on TTL, so this is optional.
            Only needed if you want to manually cleanup or get count.
        """
        # Redis handles TTL automatically, so this is mostly for logging
        try:
            active_count = self.get_active_sessions_count()
            logger.info(f"Active sessions: {active_count}")
            return 0  # Redis auto-expires
        except Exception as e:
            logger.error(f"Failed to cleanup sessions: {e}")
            return -1

    def delete_user_sessions(self, user_id: str) -> int:
        """Delete all sessions for a specific user.

        Issue #114: Invalidate old sessions on new login to prevent
        session fixation attacks and unauthorized access from old sessions.

        Args:
            user_id: User ID (OAuth2 sub claim) to delete sessions for

        Returns:
            Number of sessions deleted

        Example:
            >>> # Before creating new session on login
            >>> deleted = manager.delete_user_sessions(user_id)
            >>> logger.info(f"Invalidated {deleted} old sessions")
            >>> new_session_id = manager.create_session(user_info)

        Note:
            Uses SCAN instead of KEYS for non-blocking iteration (O(1) per call).
            Uses pipeline for atomic batch deletion.
        """
        try:
            keys_to_delete: list[str] = []

            # Use SCAN for non-blocking iteration (PR review feedback)
            # SCAN is O(1) per call vs KEYS which is O(N) and blocks Redis
            cursor = 0
            while True:
                cursor, keys = self._redis.scan(cursor, match="session:*", count=100)
                for key in keys:
                    try:
                        data = self._redis.get(key)
                        if data:
                            session_data = json.loads(data)
                            # Check if session belongs to this user
                            # Support both "sub" (OAuth2 standard) and "user_id" (internal)
                            session_user_id = session_data.get("user_id") or session_data.get("sub")
                            if session_user_id == user_id:
                                keys_to_delete.append(key)
                    except (json.JSONDecodeError, TypeError):
                        # Skip invalid session data
                        continue

                if cursor == 0:
                    break

            # Use pipeline for atomic batch deletion (PR review feedback)
            deleted_count = 0
            if keys_to_delete:
                pipe = self._redis.pipeline()
                for key in keys_to_delete:
                    pipe.delete(key)
                results = pipe.execute()
                deleted_count = sum(1 for r in results if r)

                logger.info(f"Invalidated {deleted_count} old session(s) for user: {user_id}")

            return deleted_count

        except Exception as e:
            logger.error(f"Failed to delete user sessions: {e}")
            return 0
