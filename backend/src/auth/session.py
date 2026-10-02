"""Redis-based session management for Web UI.

Issue #554 (Redis) + Issue #650 (Google OAuth2 Web Integration)

Provides secure session storage using Redis with singleton pattern.
"""

import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from utils.datetime import utcnow

logger = logging.getLogger(__name__)

# Singleton Redis client cache (shared across all instances)
_redis_client_cache: dict[str, Any] = {}

# The browser session cookie (Issue #115 renamed it from ``session_id``).
SESSION_COOKIE_NAME = "kagura_session"

# The SessionManager the app started with (#1809). It lives here, beside the
# store, so code outside the routes (the OAuth2 server, account erasure, the
# middleware) does not import a route module to reach it.
_active_session_manager: "SessionManager | None" = None


def set_session_manager(manager: "SessionManager | None") -> None:
    """Register the app's SessionManager (called once at startup)."""
    global _active_session_manager
    _active_session_manager = manager


def get_session_manager() -> "SessionManager | None":
    """Return the app's SessionManager, or None before startup registered one."""
    return _active_session_manager


# ---------------------------------------------------------------------------
# Per-user session index (#1809)
# ---------------------------------------------------------------------------
# ``user_sessions:<id>`` is a set of the session ids that hold account ``<id>``,
# so signing a user out reads that user's sessions instead of walking every
# ``session:*`` key. The prefix must not start with ``session:``, or a SCAN for
# sessions would pick the sets up.
#
# Every write that keeps a session alive (create, a read that renews the TTL,
# an account change, an update) also adds the session to the sets of every id
# it answers to, and renews their TTL to the session's. A set therefore lives
# at least as long as its newest member. Ids whose session expired or was
# deleted stay in the set until the next sweep of that user prunes them.
#
# Sessions written before the index existed have no entry. Two things keep
# them reachable:
#   1. a read indexes the session, so any session that is used after the
#      deploy is in the index from its next request on;
#   2. for one session lifetime (+ a margin for a rolling deploy) after the
#      first sweep that ran this code, the sweep still SCANs ``session:*`` as
#      well. A session not used in that time has expired by the end of it.
# The window start is a marker key written once (SET NX) and never expired.
# Rolling back to a release without the index and then forward again leaves
# the sessions written meanwhile unindexed: delete the marker on the redeploy
# so the window starts over.
_USER_INDEX_PREFIX = "user_sessions:"
_LEGACY_SCAN_MARKER = "session_index:since"
_LEGACY_SCAN_MARGIN_SECONDS = 24 * 3600


def _user_index_key(user_id: str) -> str:
    return f"{_USER_INDEX_PREFIX}{user_id}"


def browser_cookie_attrs() -> dict[str, Any]:
    """Attributes shared by every cookie the API sets on the browser.

    The session cookie and the device cookie (#1769) must not diverge — #1487
    was an open-coded ``set_cookie`` that shipped without ``Secure`` in
    production. ``Secure`` follows ``ENVIRONMENT=production``.
    """
    return {
        "path": "/",
        "httponly": True,
        "secure": os.getenv("ENVIRONMENT", "development") == "production",
        "samesite": "lax",
    }


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
_ENVELOPE_KEYS = frozenset(
    {"v", "accounts", "active", "created_at", "last_accessed", "updated_at", "signed_in_at"}
)

# When each account last went through a sign-in (#1803): ``{account_id: iso}``
# on the container, never on an identity. An account stays in a container for
# as long as the session lives, so membership alone says nothing about when it
# proved itself; an identity link asks for a recent sign-in of both accounts.
# Only ``create_session`` and ``add_account`` (a sign-in) write it — switching
# accounts, refreshing the TTL and ``update_session`` never do. A record
# written before this key existed has no time for any account, which readers
# treat as "not recent" (fail closed).
_SIGNED_IN_AT = "signed_in_at"


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


def _index_ids(container: dict[str, Any]) -> set[str]:
    """Every id a sweep may ask for this container by.

    The account keys, plus each identity's ``user_id`` and ``sub`` — the same
    ids ``session_owns_user`` matches on, so the index can answer every
    question the scan could.
    """
    ids: set[str] = set()
    for key, identity in container.get("accounts", {}).items():
        if isinstance(key, str) and key:
            ids.add(key)
        if isinstance(identity, dict):
            for field in ("user_id", "sub"):
                value = identity.get(field)
                if isinstance(value, str) and value:
                    ids.add(value)
    return ids


def _record_belongs(record: Any, user_id: str) -> bool:
    """Does a stored session record (either shape) hold ``user_id``? (#114)

    #1488: a container nests identities under ``accounts``, so reading
    ``user_id``/``sub`` off the top level would match NOTHING and silently
    retire #114's one-session-per-user guarantee. Match on account MEMBERSHIP,
    which is also the right question once a container can hold several.
    Legacy flat records are still matched the old way.
    """
    if not isinstance(record, dict):
        return False
    if is_container(record):
        return session_owns_user(record, user_id)
    # Support both "sub" (OAuth2) and "user_id" (internal)
    return (record.get("user_id") or record.get("sub")) == user_id


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
        # Set once a sweep has seen the legacy-scan window close (#1809).
        self._legacy_scan_over = False

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
            _SIGNED_IN_AT: {account_id: now},
        }

        # Store in Redis with TTL, and index it in the same transaction: a
        # session that cannot be found by its user cannot be signed out.
        try:
            pipe = self._redis.pipeline()
            pipe.setex(
                f"session:{session_id}",
                self.session_ttl,
                json.dumps(session_data),
            )
            self._queue_index(pipe, session_id, session_data)
            pipe.execute()
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
                # One round trip, NOT a transaction (#1809): the renewal and
                # the index writes run together, so the index set's TTL moves
                # with the session's, but an index command Redis refuses (a
                # SADD at maxmemory, say) does not undo the renewal or turn a
                # valid session into a signed-out one.
                pipe = self._redis.pipeline(transaction=False)
                if was_legacy:
                    # One-time: a flat record must be written once to become a
                    # container. The only write left on the read path, and it
                    # happens at most once per record. XX: a sweep that deleted
                    # the record since the read above must not be undone (#1809).
                    pipe.set(
                        f"session:{session_id}",
                        json.dumps(container),
                        ex=self.session_ttl,
                        xx=True,
                    )
                else:
                    pipe.expire(f"session:{session_id}", self.session_ttl)
                # Renewing the session renews its index entries, and indexes a
                # session written before the index existed (#1809).
                self._queue_index(pipe, session_id, container)
                results = pipe.execute(raise_on_error=False)
                if results and isinstance(results[0], Exception):
                    raise results[0]
                index_errors = [r for r in results[1:] if isinstance(r, Exception)]
                if index_errors:
                    logger.warning(f"Failed to refresh session index: {index_errors[0]}")
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
            raw = self._redis.get(f"session:{session_id}")
            deleted = self._redis.delete(f"session:{session_id}")
            if deleted:
                logger.info(f"Deleted session: {session_id[:10]}...")
            if raw:
                self._drop_from_index(session_id, raw)
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

            before = _index_ids(container)
            if mutate(container) is False:
                return False

            container["last_accessed"] = utcnow().isoformat()
            return self._write_existing(
                session_id, container, dropped=before - _index_ids(container)
            )
        except Exception as e:
            logger.error(f"Failed to mutate session: {e}")
            return False

    def _drop_from_index(self, session_id: str, raw: Any) -> None:
        """Remove a deleted session from its index sets, best effort (#1809)."""
        try:
            stored = json.loads(raw)
            container = stored if is_container(stored) else to_container(stored)
        except Exception as e:
            logger.warning(f"Failed to drop session from index: {e}")
            return
        self._drop_ids_from_index(session_id, _index_ids(container))

    def _drop_ids_from_index(self, session_id: str, index_ids: set[str]) -> None:
        """SREM ``session_id`` from each listed index set, best effort (#1809)."""
        if not index_ids:
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            for index_id in index_ids:
                pipe.srem(_user_index_key(index_id), session_id)
            pipe.execute()
        except Exception as e:
            logger.warning(f"Failed to drop session from index: {e}")

    def _queue_index(self, pipe: Any, session_id: str, record: dict[str, Any]) -> None:
        """Queue the index writes for a session on ``pipe`` (#1809)."""
        container = record if is_container(record) else to_container(record)
        for index_id in _index_ids(container):
            key = _user_index_key(index_id)
            pipe.sadd(key, session_id)
            pipe.expire(key, self.session_ttl)

    def _write_existing(
        self,
        session_id: str,
        container: dict[str, Any],
        *,
        dropped: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        """Write a read-modify-write result back, unless the record is gone.

        ``SET XX``: a sweep (#114, a password reset) that deleted the record
        between our read and this write must win — a plain SETEX here would
        bring a signed-out session back to life (#1809). ``dropped`` names ids
        the container no longer answers to; they leave the index.
        """
        pipe = self._redis.pipeline()
        pipe.set(
            f"session:{session_id}",
            json.dumps(container),
            ex=self.session_ttl,
            xx=True,
        )
        self._queue_index(pipe, session_id, container)
        for index_id in dropped:
            pipe.srem(_user_index_key(index_id), session_id)
        results = pipe.execute()
        if not results or not results[0]:
            logger.warning(f"Session vanished before the write: {session_id[:10]}...")
            # The SADDs above ran anyway (one MULTI); take the dead id back out.
            self._drop_ids_from_index(session_id, _index_ids(container) | set(dropped))
            return False
        return True

    def session_holds_user(self, session_id: str, user_id: str) -> bool:
        """Is ``user_id`` one of the accounts signed in on this session? (#1770)

        The membership rule ``delete_user_sessions`` applies, so a grant
        writer re-checking its session after a reset agrees with the reset:
        the whole container goes when any of its accounts resets, and an
        account that merely stopped being the active one is still signed in.
        Reads the record without refreshing its TTL. Missing, unusable or
        unreadable → False.
        """
        try:
            raw = self._redis.get(f"session:{session_id}")
            if not raw:
                return False
            stored = json.loads(raw)  # type: ignore[arg-type]
            container = stored if is_container(stored) else to_container(stored)
            if project_active(container) is None:
                return False
            return session_owns_user(container, user_id)
        except Exception as e:
            logger.error(f"Failed to check session membership: {e}")
            return False

    def signed_in_at(self, session_id: str, account_id: str) -> datetime | None:
        """When ``account_id`` last signed in on this session (#1803).

        Naive UTC, like ``utcnow()``. None when the session is missing or
        unusable, the account is not in it, or no readable time was recorded
        for it (a record from before the time was kept). Reads the record
        without refreshing its TTL.
        """
        try:
            raw = self._redis.get(f"session:{session_id}")
            if not raw:
                return None
            stored = json.loads(raw)  # type: ignore[arg-type]
            if not is_container(stored) or project_active(stored) is None:
                return None
            if account_id not in stored.get("accounts", {}):
                return None
            signed_in = stored.get(_SIGNED_IN_AT)
            value = signed_in.get(account_id) if isinstance(signed_in, dict) else None
            if not isinstance(value, str):
                return None
            when = datetime.fromisoformat(value)
            return when if when.tzinfo is None else None
        except Exception as e:
            logger.error(f"Failed to read sign-in time: {e}")
            return None

    def signed_in_within(self, session_id: str, account_id: str, window: timedelta) -> bool:
        """Whether ``account_id`` signed in on this session within ``window``.

        A time in the future (clock skew, a tampered record) does not count,
        and neither does a missing one.
        """
        when = self.signed_in_at(session_id, account_id)
        if when is None:
            return False
        age = utcnow() - when
        return timedelta(0) <= age <= window

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
            # A sign-in: the one place besides create_session that sets it.
            signed_in = container.get(_SIGNED_IN_AT)
            if not isinstance(signed_in, dict):
                signed_in = container[_SIGNED_IN_AT] = {}
            signed_in[account_id] = utcnow().isoformat()

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
            signed_in = container.get(_SIGNED_IN_AT)
            if isinstance(signed_in, dict):
                signed_in.pop(account_id, None)
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
            before = _index_ids(container)
            active = container.get("active", "")
            identity = dict(container.get("accounts", {}).get(active, {}))
            for key, value in updates.items():
                if key == _SIGNED_IN_AT:
                    # Only a sign-in sets it (#1803); an update is not one.
                    continue
                if key in _ENVELOPE_KEYS:
                    container[key] = value
                else:
                    identity[key] = value
            container.setdefault("accounts", {})[active] = identity
            container["updated_at"] = utcnow().isoformat()

            # Save back to Redis — only if it is still there (#1809).
            if not self._write_existing(
                session_id, container, dropped=before - _index_ids(container)
            ):
                return False

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
            # SCAN, not KEYS: KEYS walks the whole keyspace in one blocking
            # call (#1809).
            count = 0
            cursor = 0
            while True:
                cursor, keys = self._redis.scan(cursor, match="session:*", count=500)
                count += len(keys)
                if cursor == 0:
                    return count
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

    def _legacy_scan_needed(self) -> bool:
        """Must a sweep still SCAN for sessions written before the index? (#1809)

        True for ``session_ttl`` + a margin after the first sweep that ran
        this code (it writes the start marker). Anything unexpected — the
        marker unreadable, Redis refusing the write — answers True: a slower
        sweep is better than a session that cannot be signed out.
        """
        if self._legacy_scan_over:
            return False
        try:
            now = time.time()
            self._redis.set(_LEGACY_SCAN_MARKER, str(now), nx=True)
            raw = self._redis.get(_LEGACY_SCAN_MARKER)
            if not isinstance(raw, str | bytes):
                return True
            since = float(raw)
        except Exception:
            return True
        if now < since + self.session_ttl + _LEGACY_SCAN_MARGIN_SECONDS:
            return True
        self._legacy_scan_over = True
        return False

    def delete_user_sessions(
        self,
        user_id: str,
        exclude_session_id: str | None = None,
        *,
        strict: bool = False,
    ) -> int:
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
            strict: Re-raise a Redis failure instead of reporting 0 deleted.
                The password flows (#1678) must not report success — or
                commit the new password — when the old sessions survived.

        Returns:
            Number of sessions deleted

        Raises:
            Exception: Only with ``strict=True``: whatever Redis raised.

        Example:
            >>> # Before creating new session on login
            >>> deleted = manager.delete_user_sessions(user_id)
            >>> logger.info(f"Invalidated {deleted} old sessions")
            >>> new_session_id = manager.create_session(user_info)

        Note:
            Candidates come from the user's index set (#1809). For one
            session lifetime after the index first appeared, a SCAN of
            ``session:*`` adds sessions written before it (see
            ``_legacy_scan_needed``). Every candidate is re-read and kept
            only if its record still holds the user; ids whose session is
            gone are pruned from the index.
        """
        excluded_key = f"session:{exclude_session_id}" if exclude_session_id else None
        index_key = _user_index_key(user_id)
        try:
            candidates: set[str] = {
                f"session:{sid}" for sid in (self._redis.smembers(index_key) or ())
            }
            if self._legacy_scan_needed():
                cursor = 0
                while True:
                    cursor, keys = self._redis.scan(cursor, match="session:*", count=100)
                    candidates.update(keys)
                    if cursor == 0:
                        break

            keys_to_delete: list[str] = []
            # Every index set a deleted container sits in, not only this user's:
            # its other accounts' sets must not keep a dead id (#1809).
            other_sets: dict[str, set[str]] = {}
            stale_ids: list[str] = []
            for key in candidates:
                if excluded_key is not None and key == excluded_key:
                    continue
                data = self._redis.get(key)
                if not data:
                    stale_ids.append(key.removeprefix("session:"))
                    continue
                try:
                    record = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    # Skip invalid session data
                    continue
                if _record_belongs(record, user_id):
                    keys_to_delete.append(key)
                    try:
                        container = record if is_container(record) else to_container(record)
                        other_sets[key] = _index_ids(container) - {user_id}
                    except Exception:
                        other_sets[key] = set()

            # One transaction: the sessions and their index entries go together.
            deleted_count = 0
            if keys_to_delete or stale_ids:
                pipe = self._redis.pipeline()
                for key in keys_to_delete:
                    pipe.delete(key)
                for sid in stale_ids + [k.removeprefix("session:") for k in keys_to_delete]:
                    pipe.srem(index_key, sid)
                for key, index_ids in other_sets.items():
                    for index_id in index_ids:
                        pipe.srem(_user_index_key(index_id), key.removeprefix("session:"))
                results = pipe.execute()
                deleted_count = sum(1 for r in results[: len(keys_to_delete)] if r)

            if deleted_count:
                logger.info(f"Invalidated {deleted_count} old session(s) for user: {user_id}")

            return deleted_count

        except Exception as e:
            logger.error(f"Failed to delete user sessions: {e}")
            if strict:
                raise
            return 0
