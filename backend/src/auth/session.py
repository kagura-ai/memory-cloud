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

# ---------------------------------------------------------------------------
# Session record shape (#1488 Phase 1)
# ---------------------------------------------------------------------------
# A session record used to be a FLAT dict: the identity fields merged with
# `created_at` / `last_accessed`. That shape can only ever describe one account,
# which is the first of the three things blocking multi-account switching.
#
# Records are now stored as a CONTAINER:
#
#     {"v": 2,
#      "accounts": {"<user_id>": {identity...}},
#      "active": "<user_id>",
#      "created_at": ..., "last_accessed": ...}
#
# Phase 1 puts exactly one account in it and changes NOTHING a caller can see:
# `get_session()` still returns the flat shape, projected from the active
# account. That matters because ~30 route handlers read `user["user_id"]`
# straight off `request.state.user`; making them account-aware is Phase 2's job,
# not a refactor's.
#
# Legacy flat records already in Redis are read transparently — a deploy must
# not log everyone out — and are rewritten as containers the next time the
# record is touched.
_SESSION_VERSION = 2

# Envelope keys belong to the container, not to an account identity.
_ENVELOPE_KEYS = frozenset({"v", "accounts", "active", "created_at", "last_accessed", "updated_at"})


def _account_id(identity: dict[str, Any]) -> str | None:
    """Identify an account the way every reader already does.

    `user_id` is the internal id and `sub` the OAuth2 claim; the codebase treats
    either as the account key (see `delete_user_sessions`), so this must too or
    the container would key some accounts under a different name than the code
    that looks them up.
    """
    return identity.get("user_id") or identity.get("sub")


def is_container(data: dict[str, Any]) -> bool:
    """True when a record is already in the v2 container shape."""
    return isinstance(data.get("accounts"), dict) and "active" in data


def session_owns_user(container: dict[str, Any], user_id: str) -> bool:
    """Does this container hold a session for ``user_id``? (#114)

    Deliberately checks the account KEY *and* each identity's `user_id`/`sub`,
    rather than trusting the key alone.

    Today every login writes `sub == user_id` (see the three create_session call
    sites), so the key always matches and the extra check is redundant. It is
    here because the failure mode if that ever stops holding is SILENT: an
    account keyed one way and a deletion requested the other way would make
    this return False, `delete_user_sessions` would return 0, log "invalidated
    0 sessions", and the previous session would survive the login — quietly
    reopening the session-fixation window #114 exists to close. Nothing would
    fail, so nothing would be noticed.

    Matching on identity content as well makes the guarantee independent of the
    keying choice.
    """
    accounts = container.get("accounts", {})
    if user_id in accounts:
        return True
    return any(
        isinstance(identity, dict)
        and (identity.get("user_id") == user_id or identity.get("sub") == user_id)
        for identity in accounts.values()
    )


def to_container(flat: dict[str, Any]) -> dict[str, Any]:
    """Lift a legacy flat record into the container shape.

    Envelope timestamps are preserved rather than reset: a migrated session must
    not look newly created, or session-age reporting silently lies after deploy.
    """
    identity = {k: v for k, v in flat.items() if k not in _ENVELOPE_KEYS}
    account_id = _account_id(identity) or ""
    now = utcnow().isoformat()
    container: dict[str, Any] = {
        "v": _SESSION_VERSION,
        "accounts": {account_id: identity},
        "active": account_id,
        "created_at": flat.get("created_at", now),
        "last_accessed": flat.get("last_accessed", now),
    }
    # `updated_at` is an envelope key, so it was stripped from the identity
    # above. Carry it across explicitly or migration silently loses it for any
    # record that update_session had touched.
    if "updated_at" in flat:
        container["updated_at"] = flat["updated_at"]
    return container


def project_active(container: dict[str, Any]) -> dict[str, Any] | None:
    """Return the flat view callers expect, or None if the record is unusable.

    Returns None — not an empty-ish dict — when `active` names an account that
    is not present, or the active identity carries no id.

    That distinction is the whole point. `SessionMiddleware` only checks for a
    falsy result before setting `request.state.user`, and `get_current_user`
    only rejects None. A dict holding just `created_at`/`last_accessed` is
    TRUTHY, so returning one would authenticate a principal with no `user_id`
    and no `sub` — routes would then either 500 on `user["user_id"]` or run
    authorization against an empty identity. A corrupt record must log the user
    out, which means None.
    """
    identity = container.get("accounts", {}).get(container.get("active"))
    if not isinstance(identity, dict) or not _account_id(identity):
        return None
    return {
        **identity,
        "created_at": container.get("created_at"),
        "last_accessed": container.get("last_accessed"),
    }


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

        # Session data — stored as a container holding exactly one account
        # (#1488 Phase 1). Callers still see the flat shape via get_session().
        now = utcnow().isoformat()
        account_id = _account_id(user_info) or ""
        session_data = {
            "v": _SESSION_VERSION,
            "accounts": {account_id: dict(user_info)},
            "active": account_id,
            "created_at": now,
            "last_accessed": now,
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

            stored = json.loads(data)  # type: ignore[arg-type]

            # Read BOTH shapes (#1488). Sessions minted before this change are
            # flat and still live in Redis; refusing them would log every
            # signed-in user out the moment this deploys.
            was_legacy = not is_container(stored)
            container = stored if not was_legacy else to_container(stored)

            # A record whose active account is missing or id-less is corrupt;
            # treat it as no session at all rather than authenticating a shell.
            # Checked BEFORE the refresh so a corrupt record is not also given a
            # fresh 7-day TTL.
            projected = project_active(container)
            if projected is None:
                logger.warning(f"Discarding unusable session record: {session_id[:10]}...")
                return None

            # Refresh the rolling TTL.
            #
            # This used to SETEX the whole record just to bump `last_accessed`.
            # That is an unlocked read-modify-write, and it was safe only while
            # a container held ONE account. With two, any ordinary request could
            # read first, write second, and silently discard a concurrent
            # "add account" — and since a request arrives on every page load,
            # that race would be routine rather than theoretical. Phase 1 flagged
            # this as the prerequisite for Phase 2; this is it.
            #
            # EXPIRE renews the TTL without touching the value, so the hot path
            # no longer writes the record and there is nothing to clobber.
            #
            # The trade, stated plainly: STORED `last_accessed` now advances on
            # writes rather than on every request. Nothing reads it — no route,
            # no service, no UI; only a docstring mentions it — so this costs
            # nothing today, and the returned projection still reports the
            # current time so callers see an accurate value. If true
            # per-request last-access is ever needed it must be added
            # deliberately with an atomic mechanism, not by restoring the
            # whole-record rewrite.
            if update_access:
                if was_legacy:
                    # One-time: a flat record must be written once to become a
                    # container. The only write left on the read path, and it
                    # happens at most once per record.
                    self._redis.setex(
                        f"session:{session_id}",
                        self.session_ttl,
                        json.dumps(container),
                    )
                else:
                    self._redis.expire(f"session:{session_id}", self.session_ttl)
                projected["last_accessed"] = utcnow().isoformat()

            # Callers see the flat shape they always have.
            return projected

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

    # ------------------------------------------------------------------
    # Multi-account operations (#1488 Phase 2)
    # ------------------------------------------------------------------
    #
    # These are the only writers that change `accounts` or `active`. They all
    # go through _mutate_container so the read-modify-write lives in ONE place;
    # the hot read path no longer writes at all (see get_session), so these are
    # the only writers that can race each other. They are rare — a login or an
    # explicit switch — and each is a single user action, so last-writer-wins
    # between two of them is acceptable in a way it was not for every page load.

    def _mutate_container(self, session_id: str, mutate) -> bool:
        """Read a session, apply ``mutate`` to the container, write it back.

        Returns False when the session is missing or unusable. ``mutate`` may
        return False to abort the write.
        """
        try:
            raw = self._redis.get(f"session:{session_id}")
            if not raw:
                return False
            stored = json.loads(raw)  # type: ignore[arg-type]
            container = stored if is_container(stored) else to_container(stored)

            # Same rule as get_session/update_session: a record whose active
            # account cannot be resolved is unusable, and writing to it would
            # only corrupt it further.
            if project_active(container) is None:
                logger.warning(f"Refusing to mutate unusable session: {session_id[:10]}...")
                return False

            if mutate(container) is False:
                return False

            container["last_accessed"] = utcnow().isoformat()
            self._redis.setex(
                f"session:{session_id}",
                self.session_ttl,
                json.dumps(container),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to mutate session: {e}")
            return False

    def list_accounts(self, session_id: str) -> list[dict[str, Any]]:
        """Identities signed in on this session, active one flagged.

        Returns [] for a missing or unusable session — the UI shows no switcher
        rather than an error, which is the right failure for a menu.
        """
        try:
            raw = self._redis.get(f"session:{session_id}")
            if not raw:
                return []
            stored = json.loads(raw)  # type: ignore[arg-type]
            container = stored if is_container(stored) else to_container(stored)
            if project_active(container) is None:
                return []
            active = container.get("active")
            return [
                {**identity, "is_active": account_id == active}
                for account_id, identity in container.get("accounts", {}).items()
            ]
        except Exception as e:
            logger.error(f"Failed to list accounts: {e}")
            return []

    def add_account(self, session_id: str, user_info: dict[str, Any]) -> bool:
        """Add an identity to an existing session and make it active.

        This is what a login performs INSTEAD of minting a fresh session when
        the browser already has one. Re-adding an account that is already
        present refreshes its identity and activates it, so "sign in again" is
        idempotent rather than creating a duplicate entry.
        """
        account_id = _account_id(user_info)
        if not account_id:
            logger.warning("Refusing to add an account with no id")
            return False

        def _add(container: dict[str, Any]) -> None:
            container.setdefault("accounts", {})[account_id] = dict(user_info)
            container["active"] = account_id

        return self._mutate_container(session_id, _add)

    def switch_account(self, session_id: str, account_id: str) -> bool:
        """Make an already-signed-in account the active one.

        Refuses an account that is not in the container. That refusal is the
        security boundary of this whole feature: without it, a caller could
        name ANY user id and the session would start acting as them. The check
        is membership in this session's own container — never a lookup.
        """

        def _switch(container: dict[str, Any]) -> bool:
            if account_id not in container.get("accounts", {}):
                logger.warning(
                    f"Refusing to switch session {session_id[:10]}... to a non-member account"
                )
                return False
            container["active"] = account_id
            return True

        return self._mutate_container(session_id, _switch)

    def remove_account(self, session_id: str, account_id: str) -> bool:
        """Sign one account out, leaving the others signed in.

        Removing the LAST account leaves nothing to be active, so the whole
        session is deleted — that is a full sign-out, and it must not leave an
        empty container behind for `project_active` to reject on every request.
        Removing the ACTIVE account promotes an arbitrary remaining one.
        """
        try:
            raw = self._redis.get(f"session:{session_id}")
            if not raw:
                return False
            stored = json.loads(raw)  # type: ignore[arg-type]
            container = stored if is_container(stored) else to_container(stored)
            accounts = container.get("accounts", {})
            if account_id not in accounts:
                return False
            if len(accounts) <= 1:
                return self.delete_session(session_id)
        except Exception as e:
            logger.error(f"Failed to read session for account removal: {e}")
            return False

        def _remove(container: dict[str, Any]) -> None:
            accounts = container.get("accounts", {})
            accounts.pop(account_id, None)
            if container.get("active") == account_id:
                container["active"] = next(iter(accounts))

        return self._mutate_container(session_id, _remove)

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
            # Read the RAW record, not the flat projection: writing a projection
            # back would collapse the container and drop every non-active
            # account (#1488).
            raw = self._redis.get(f"session:{session_id}")
            if not raw:
                return False
            stored = json.loads(raw)  # type: ignore[arg-type]
            container = stored if is_container(stored) else to_container(stored)

            # Refuse a record whose active account is missing or id-less, for
            # the same reason get_session discards it. Without this the
            # `setdefault(...)[active] = identity` below would CREATE a new
            # empty account under the dangling pointer — corrupting the record
            # further while reporting success.
            if project_active(container) is None:
                logger.warning(f"Refusing to update unusable session: {session_id[:10]}...")
                return False

            # Updates apply to the ACTIVE account's identity. Envelope keys are
            # the container's, so they are set on the container instead — a
            # caller passing `created_at` must not end up with it nested inside
            # an identity where nothing reads it.
            active = container.get("active", "")
            identity = dict(container.get("accounts", {}).get(active, {}))
            for key, value in updates.items():
                if key in _ENVELOPE_KEYS:
                    container[key] = value
                else:
                    identity[key] = value
            container.setdefault("accounts", {})[active] = identity
            container["updated_at"] = utcnow().isoformat()

            # Save back to Redis
            self._redis.setex(
                f"session:{session_id}",
                self.session_ttl,
                json.dumps(container),
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

    def delete_user_sessions(self, user_id: str, exclude_session_id: str | None = None) -> int:
        """Delete all sessions for a specific user.

        Issue #114: Invalidate old sessions on new login to prevent
        session fixation attacks and unauthorized access from old sessions.

        Args:
            user_id: User ID (OAuth2 sub claim) to delete sessions for
            exclude_session_id: A session to spare. Required by the
                "add another account" login (#1488): that flow APPENDS to a
                live session, and this method deletes whole CONTAINERS by
                membership — so without an exclusion, re-authenticating an
                identity the container already holds would destroy the very
                session being added to, evicting every other account with it.
                #114 still holds: every OTHER session for the user is deleted.

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
        excluded_key = f"session:{exclude_session_id}" if exclude_session_id else None
        try:
            keys_to_delete: list[str] = []

            # Use SCAN for non-blocking iteration (PR review feedback)
            # SCAN is O(1) per call vs KEYS which is O(N) and blocks Redis
            cursor = 0
            while True:
                cursor, keys = self._redis.scan(cursor, match="session:*", count=100)
                for key in keys:
                    if excluded_key is not None and key == excluded_key:
                        continue
                    try:
                        data = self._redis.get(key)
                        if data:
                            session_data = json.loads(data)
                            # Check if session belongs to this user.
                            #
                            # #1488: a container nests identities under
                            # `accounts`, so reading `user_id`/`sub` off the top
                            # level would match NOTHING and silently retire
                            # #114's one-session-per-user guarantee. Match on
                            # account MEMBERSHIP, which is also the right
                            # question once a container can hold several.
                            #
                            # Legacy flat records are still matched the old way
                            # — they are what is in Redis at deploy time.
                            if is_container(session_data):
                                belongs = session_owns_user(session_data, user_id)
                            else:
                                # Support both "sub" (OAuth2) and "user_id" (internal)
                                belongs = (
                                    session_data.get("user_id") or session_data.get("sub")
                                ) == user_id
                            if belongs:
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
