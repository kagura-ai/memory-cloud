"""Async SQLAlchemy-based API Key Manager.

Rewritten for memory-cloud async architecture.
Manages API keys using AsyncSession and async/await patterns.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from auth.connector_keys import CONNECTOR_KEY_PREFIX
from config.settings import get_settings
from models.auth import APIKey, Context, Workspace, WorkspaceMember
from utils.datetime import utcnow
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# API Key prefix for easy identification
API_KEY_PREFIX = "kagura_"


class VerifiedKey(NamedTuple):
    """Result of a successful API key verification.

    Issue #626 — extended from a plain ``(user_id, workspace_id)`` 2-tuple
    so that public-bound keys (#626) and workspace-scoped keys (#169) can
    coexist without further breaking changes when more attribution shapes
    appear. **Callers MUST use attribute access** (``result.user_id``,
    ``result.workspace_id``) — positional unpacking like
    ``user_id, workspace_id = verified`` will raise ``ValueError: too
    many values to unpack`` against this 4-field tuple. All in-tree
    callers were migrated to attribute access in this change.

    Attributes:
        id: ``api_keys.id`` integer primary key — surfaced here so the
            public endpoint can populate per-key rate-limit buckets and
            ``usage_stats.api_key_id`` without a second SELECT.
        user_id: Owner OAuth2 sub.
        workspace_id: Workspace scope (Issue #169). None for global and
            for public-bound keys (mutually exclusive with bound_context_id).
        bound_context_id: Public-context attribution (Issue #626). None
            unless the key was created with a binding.
        key_prefix: The key's non-secret prefix (``api_keys.key_prefix``,
            e.g. ``kagura_abc123``). Issue #1164: surfaced onto the
            authenticated principal (``api_key_prefix``) so audit trails for
            programmatic member-management actions attribute the specific
            acting key, not just the owner user_id.
        agent_id: Issue #1275 (RFC-0002 P0-2): the agent this key is bound
            to, already verified ``active`` (suspended/retired agents are
            rejected at verify time — fail-closed kill switch). None for
            unbound keys.
        agent_enforcement_mode: The bound agent's ``enforcement_mode``
            (``shadow`` | ``enforce``), read in the same verify-time lookup
            so the binding chokepoints need no second agents query. None for
            unbound keys.
        is_connector_key: A connector's shared key (``api_keys.connector_id``
            set, #1952). Its owner is not the person on the other end of the
            connector, so the MCP handshake withholds the owner's last-used
            guardrail digest from it.
        default_context_id: #2129: the key's MCP default context
            (``api_keys.default_context_id``), read from the same row — no
            extra query. ``None`` when unset; never set on a public-bound or
            agent-bound key (CHECK constraints).
        connector_id: #2929: the connector the key is linked to
            (``api_keys.connector_id``), or ``None``.
        connector_scoped: #2929: the key is accepted only where
            ``auth.connector_key_scope`` accepts a connector key: it is linked
            to a connector, or it carries the reserved ``connector:`` name
            without a link. ``is_connector_key`` means "linked", for the
            readers that need the connector row.
    """

    id: int
    user_id: str
    workspace_id: UUID | None
    bound_context_id: UUID | None
    key_prefix: str | None = None
    agent_id: UUID | None = None
    agent_enforcement_mode: str | None = None
    is_connector_key: bool = False
    default_context_id: UUID | None = None
    public_id: str | None = None
    connector_id: UUID | None = None
    connector_scoped: bool = False


async def key_owner_holds_workspace(db: AsyncSession, *, user_id: str, workspace_id: UUID) -> bool:
    """Whether the owner of a workspace-scoped key holds that workspace (#2928).

    The workspace is live (``deleted_at IS NULL``) and ``user_id`` is its
    ``owner_user_id`` or has a ``workspace_members`` row in it, any role, with
    or without a seat on the plan. One statement on the workspace's primary
    key and the ``(workspace_id, user_id)`` unique index.

    Args:
        db: Async database session.
        user_id: The key's owner.
        workspace_id: The key's workspace.

    Returns:
        True when the key's owner holds the workspace.
    """
    holds = exists().where(
        Workspace.id == workspace_id,
        Workspace.deleted_at.is_(None),
        or_(
            Workspace.owner_user_id == user_id,
            exists().where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            ),
        ),
    )
    return bool(await db.scalar(select(holds)))


def bound_key_owner_holds(
    user_id: Any, context_id: Any, *, live_context: bool = False
) -> ColumnElement[bool]:
    """The rule of :func:`key_owner_holds_workspace` for a public-bound key.

    True when the workspace of the key's bound context is live and
    ``user_id`` is its owner or has a ``workspace_members`` row in it. A
    missing context does not hold. Both arguments are values or columns, so
    the condition also filters a query over ``api_keys`` (correlated on
    ``APIKey.user_id`` / ``APIKey.bound_context_id``).

    With ``live_context`` the bound context itself must also be live
    (``deleted_at IS NULL``): the binding introspection and a key's rotation
    leave out a binding to a soft-deleted context. The REST public search
    does not ask for it: its context check answers a soft-deleted context
    with the missing context's 404, with a key and without.

    Args:
        user_id: The key's owner (a value or a column).
        context_id: The key's ``bound_context_id`` (a value or a column).
        live_context: Also require the bound context not to be soft-deleted.
    """
    # Aliased, so an outer query over ``contexts`` or ``workspaces`` does not
    # correlate them away.
    bound, workspace = aliased(Context), aliased(Workspace)
    context_live = (bound.deleted_at.is_(None),) if live_context else ()
    return exists().where(
        bound.id == context_id,
        *context_live,
        workspace.id == bound.workspace_id,
        workspace.deleted_at.is_(None),
        or_(
            workspace.owner_user_id == user_id,
            # Correlated on every table but its own: a column ``user_id``
            # belongs to the outermost query, two levels up.
            exists()
            .where(
                WorkspaceMember.workspace_id == bound.workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .correlate_except(WorkspaceMember),
        ),
    )


async def bound_key_owner_holds_context_workspace(
    db: AsyncSession, *, user_id: str, context_id: UUID, live_context: bool = False
) -> bool:
    """Whether the owner of a public-bound key holds its context's workspace.

    :func:`bound_key_owner_holds` for one key, in one statement.

    Args:
        db: Async database session.
        user_id: The key's owner.
        context_id: The key's ``bound_context_id``.
        live_context: Also require the bound context not to be soft-deleted.

    Returns:
        True when the key's owner holds the workspace of the bound context.
    """
    held = bound_key_owner_holds(user_id, context_id, live_context=live_context)
    return bool(await db.scalar(select(held)))


def bound_to_contexts_of(workspace_ids: Any) -> ColumnElement[bool]:
    """``api_keys`` rows public-bound to a context of the given workspaces.

    Such a key has no ``workspace_id`` of its own (the DB CHECK), so a path
    that ends a workspace's keys (member removal, a workspace's rows deleted
    for good) reaches it through its context. ``bound_context_id`` is
    ``ON DELETE SET NULL``: a key left in place when its context goes would
    lose its binding, so these paths delete it before the context.

    Args:
        workspace_ids: A list of workspace ids or a select of them.
    """
    return APIKey.bound_context_id.in_(
        select(Context.id).where(Context.workspace_id.in_(workspace_ids))
    )


def member_keys_in_workspace(user_id: str, workspace_id: UUID) -> ColumnElement[bool]:
    """The ``api_keys`` rows of ``user_id`` that belong to ``workspace_id``.

    The workspace-scoped ones (plain, agent and connector keys) and the
    public-bound ones of the workspace's contexts. The member credentials
    listing shows these (it adds ``revoked_at IS NULL``) and the member's
    removal deletes them.
    """
    return and_(
        APIKey.user_id == user_id,
        or_(APIKey.workspace_id == workspace_id, bound_to_contexts_of([workspace_id])),
    )


def apply_zero_knowledge_hide(key: APIKey) -> None:
    """Apply the Migration-034/035 zero-knowledge hide mutations to ``key``.

    Stops showing the plaintext (``hidden_at`` now, ``visibility_expires_at``
    cleared) AND drops the Fernet-decryptable at-rest copy
    (``plaintext_encrypted`` nulled). Shared by ``hide_key``, the
    owner-provisioned force-hide (member_credentials), and the programmatic
    soft-revoke so the three never drift — the at-rest copy must always be
    dropped, because the hourly auto-hide sweeper skips already-hidden and
    revoked rows and would never revisit them. Does NOT flush/commit.
    """
    key.hidden_at = utcnow()
    key.visibility_expires_at = None
    key.plaintext_encrypted = None


class APIKeyManager:
    """Async API Key manager using SQLAlchemy.

    Manages API keys with async/await for memory-cloud architecture.
    """

    def __init__(self, db: AsyncSession):
        """Initialize API Key manager.

        Args:
            db: Async database session
        """
        self.db = db

    @staticmethod
    def _hash_key(api_key: str) -> str:
        """Hash API key using SHA256."""
        return sha256_hex(api_key)

    @staticmethod
    def _generate_key() -> str:
        """Generate a new API key.

        Returns:
            API key string (format: kagura_<random>)
        """
        random_part = secrets.token_urlsafe(32)
        return f"{API_KEY_PREFIX}{random_part}"

    async def create_key(
        self,
        name: str,
        user_id: str,
        expires_days: int | None = None,
        workspace_id: UUID | None = None,
        bound_context_id: UUID | None = None,
        auto_hide_minutes: int = 10,
        agent_id: UUID | None = None,
        default_context_id: UUID | None = None,
        connector_id: UUID | None = None,
    ) -> tuple[str, APIKey]:
        """Create a new API key.

        Issue #169: Workspace-scoped API keys (access all contexts in workspace).
        Issue #626: Public-bound API keys (attributed to one is_public=true context).
            Returning the freshly-flushed ``APIKey`` row alongside the plaintext
            saves callers a second SELECT just to read back ``id`` / ``created_at``
            for the one-time-reveal response.
        Migration 034: Added auto_hide for visibility control (Zero-knowledge model).
        Migration 034: Removed context_id parameter (deprecated).

        Args:
            name: Friendly name for the key
            user_id: User ID that owns this key
            expires_days: Lifetime in days. ``None`` (omitted) resolves to the
                deployment default — ``api_key_default_expires_days`` (365) or
                ``api_key_agent_default_expires_days`` (90) when ``agent_id``
                is set; ``0`` is the explicit opt-in for a key that never
                expires (Issue #1537).
            workspace_id: Optional workspace ID for workspace-scoped access (Issue #169).
                Mutually exclusive with ``bound_context_id``.
            bound_context_id: Optional context ID for public-bound attribution
                (Issue #626). The context must satisfy ``is_public=True`` at
                creation time. Mutually exclusive with ``workspace_id``.
            auto_hide_minutes: Minutes until key is auto-hidden (default: 10)
            agent_id: Issue #1275 (RFC-0002 P0-2): bind the key to a
                registered agent. Requires ``workspace_id`` (an agent-bound
                key with the global-key shape is rejected at mint) and the
                agent MUST belong to the same workspace — PostgreSQL cannot
                express this cross-table invariant in a CHECK, so it is
                enforced here and re-asserted defensively at verify. Mutually
                exclusive with ``bound_context_id``.
            default_context_id: #2129: the key's MCP default context. Stored
                as given — the caller checks the user can read it. Mutually
                exclusive with ``bound_context_id`` and ``agent_id``.
            connector_id: #1952: mint the write key of this connector
                (``api_keys.connector_id``). Requires ``workspace_id`` and the
                connector MUST belong to that workspace — enforced here (no
                cross-table CHECK) and re-asserted at verify. Mutually
                exclusive with ``bound_context_id``, ``agent_id`` and
                ``default_context_id``.

        Returns:
            ``(plaintext_key, new_api_key_row)`` — the plaintext is only ever
            available in this return value (encrypted-at-rest beyond that);
            the row carries the assigned ``id`` and ``created_at``.

        Raises:
            ValueError: If name already exists for user, if both scoping
                params are supplied, if the bound context is not public, or
                if the agent binding is malformed (missing/mismatched
                workspace, unknown agent, or a non-active agent), or if the
                connector binding is malformed (missing/mismatched workspace,
                unknown connector, or combined with another binding).
        """
        if workspace_id is not None and bound_context_id is not None:
            raise ValueError(
                "workspace_id and bound_context_id are mutually exclusive — "
                "a key cannot be both workspace-scoped and public-bound"
            )

        # #2129: mirrors ck_api_keys_default_not_bound / _not_agent.
        if default_context_id is not None and (
            bound_context_id is not None or agent_id is not None
        ):
            raise ValueError(
                "default_context_id cannot be set on a public-bound or agent-bound key"
            )

        # Issue #1275: agent-bound mint gates. Fail-closed at mint so an
        # invalid binding never reaches the verify path.
        if agent_id is not None:
            if bound_context_id is not None:
                raise ValueError(
                    "agent_id and bound_context_id are mutually exclusive — "
                    "a key cannot be both agent-bound and public-bound"
                )
            if workspace_id is None:
                raise ValueError(
                    "agent-bound keys must be workspace-scoped — a global key cannot carry agent_id"
                )
            from models.agent import AGENT_STATUS_ACTIVE, Agent

            agent_result = await self.db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                raise ValueError(f"Agent {agent_id} not found")
            if agent.workspace_id != workspace_id:
                raise ValueError(
                    "agent and key workspace mismatch — the agent must belong "
                    "to the key's workspace"
                )
            if agent.status != AGENT_STATUS_ACTIVE:
                raise ValueError(
                    f"agent is '{agent.status}' — keys can only be minted for active agents"
                )

        # #1952: connector write key — mirrors ck_api_keys_connector_* and the
        # agent gates above (cross-table workspace equality checked here).
        if connector_id is not None:
            if workspace_id is None:
                raise ValueError(
                    "connector keys must be workspace-scoped — a global key "
                    "cannot carry connector_id"
                )
            if (
                agent_id is not None
                or bound_context_id is not None
                or default_context_id is not None
            ):
                raise ValueError(
                    "connector_id cannot be combined with agent_id, "
                    "bound_context_id or default_context_id"
                )
            from models.resource import WorkspaceConnector

            connector_result = await self.db.execute(
                select(WorkspaceConnector.workspace_id).where(WorkspaceConnector.id == connector_id)
            )
            connector_workspace_id = connector_result.scalar_one_or_none()
            if connector_workspace_id is None:
                raise ValueError(f"Connector {connector_id} not found")
            if connector_workspace_id != workspace_id:
                raise ValueError(
                    "connector and key workspace mismatch — the connector must "
                    "belong to the key's workspace"
                )

        # Validate the bound context exists and is public at creation time.
        # Subsequent flips of context.is_public are handled at request time
        # by the public endpoint (the binding row stays; access is denied
        # while is_public is false).
        if bound_context_id is not None:
            # A soft-deleted context reads as a missing one.
            ctx_result = await self.db.execute(
                select(Context).where(Context.id == bound_context_id, Context.deleted_at.is_(None))
            )
            ctx = ctx_result.scalar_one_or_none()
            if ctx is None:
                raise ValueError(f"Context {bound_context_id} not found")
            if ctx.is_public is not True:
                raise ValueError(
                    "Cannot bind API key to a non-public context — "
                    "set is_public=true on the context first"
                )

        # Check if name already exists in current workspace (Issue #169)
        conditions = [
            APIKey.name == name,
            APIKey.user_id == user_id,
            APIKey.revoked_at.is_(None),
        ]
        if workspace_id:
            conditions.append(APIKey.workspace_id == workspace_id)

        result = await self.db.execute(select(APIKey).where(and_(*conditions)))
        existing = result.scalar_one_or_none()

        if existing:
            # The uniqueness query above scopes to workspace_id only when
            # one is provided (#169 keys); for owner-scoped and public-bound
            # keys it scopes to (user_id, active). Tail the message to match.
            scope_phrase = "in this workspace" if workspace_id is not None else "for this user"
            raise ValueError(f"API key with name '{name}' already exists {scope_phrase}")

        # Generate new key
        api_key = self._generate_key()
        key_hash = self._hash_key(api_key)
        key_prefix = api_key[:16]

        # Issue #1537: keys expire by default. None = "use the deployment default"
        # (agent-bound keys get the shorter one); 0 = explicit never-expires
        # opt-in, which the falsy check below turns into expires_at=None.
        if expires_days is None:
            settings = get_settings()
            expires_days = (
                settings.api_key_agent_default_expires_days
                if agent_id is not None
                else settings.api_key_default_expires_days
            )

        # Calculate expiration
        expires_at = None
        if expires_days:
            expires_at = utcnow() + timedelta(days=expires_days)

        # Calculate visibility expiration (Migration 034) - 10 minutes default
        visibility_expires_at = utcnow() + timedelta(minutes=auto_hide_minutes)

        # Migration 035: Encrypt plaintext for storage
        from utils.encryption import get_encryptor

        plaintext_encrypted = get_encryptor().encrypt(api_key)

        # Create database record
        new_key = APIKey(
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=name,
            user_id=user_id,
            workspace_id=workspace_id,  # Issue #169
            bound_context_id=bound_context_id,  # Issue #626
            agent_id=agent_id,  # Issue #1275
            default_context_id=default_context_id,  # Issue #2129
            connector_id=connector_id,  # Issue #1952
            expires_at=expires_at,
            visibility_expires_at=visibility_expires_at,  # Migration 034
            plaintext_encrypted=plaintext_encrypted,  # Migration 035
        )

        self.db.add(new_key)
        await self.db.flush()

        logger.info(
            "api_key_created",
            name=name,
            user_id=user_id,
            workspace_id=str(workspace_id) if workspace_id else None,
            bound_context_id=str(bound_context_id) if bound_context_id else None,
            agent_id=str(agent_id) if agent_id else None,
            connector_id=str(connector_id) if connector_id else None,
        )

        return api_key, new_key

    async def verify_key(self, api_key: str) -> VerifiedKey | None:
        """Verify API key and return a ``VerifiedKey`` view.

        Issue #169: Returns workspace_id for workspace-scoped API keys.
        Issue #626: Returns bound_context_id for public-bound API keys.
        Migration 034: Removed context_id (deprecated).

        Args:
            api_key: Plaintext API key to verify

        Returns:
            ``VerifiedKey`` if valid, None if not found / revoked / expired, or
            workspace-scoped while its owner does not hold the live workspace.

            - Owner-scoped (global) keys: workspace_id=None, bound_context_id=None
            - Workspace-scoped keys (#169): workspace_id=<UUID>, bound_context_id=None
            - Public-bound keys (#626): workspace_id=None, bound_context_id=<UUID>

            The two non-null scopings are mutually exclusive (DB CHECK constraint).
        """
        key_hash = self._hash_key(api_key)

        result = await self.db.execute(select(APIKey).where(APIKey.key_hash == key_hash))
        key_record = result.scalar_one_or_none()

        if not key_record:
            return None

        # Issue #964: the SELECT above filters by key_hash in the DB B-tree,
        # which leaks an application-level query-timing oracle (a prefix-matching
        # hash takes measurably longer than a total miss). Re-gate acceptance on
        # a constant-time comparison before trusting the surfaced row, so a
        # co-located adversary cannot use response timing to search the hash
        # space. Both operands are fixed-length sha256 hex strings.
        if not secrets.compare_digest(key_record.key_hash, key_hash):
            return None

        # Check if revoked
        if key_record.revoked_at:
            return None

        # Check if expired
        if key_record.expires_at:
            if utcnow() > key_record.expires_at:
                return None

        # #2928: a workspace-scoped key (plain, agent or connector) verifies
        # only while its owner holds the live workspace. Checked before the
        # connector and agent gates below, so a refused key writes nothing (no
        # agent liveness touch, no last_used_at); those gates keep their own
        # answers for a key whose owner does hold the workspace. Global and
        # public-bound keys have no workspace and skip it.
        if key_record.workspace_id is not None and not await key_owner_holds_workspace(
            self.db, user_id=key_record.user_id, workspace_id=key_record.workspace_id
        ):
            logger.warning(
                "api_key_rejected_owner_not_in_workspace",
                key_prefix=key_record.key_prefix,
                workspace_id=str(key_record.workspace_id),
            )
            return None

        # #1944: a connector's write key stops with the connector it serves.
        # #1939 pauses connector ingest (REST/MCP ingest, worker config,
        # indexer) while the workspace has no connector seats, but the worker
        # also holds this workspace key for its legacy ``remember`` fallback,
        # so it is refused while ``connectors_suspended``. #1952: the
        # ``api_keys.connector_id`` FK is the link and decides this pause (and
        # the MCP notices and default context); a key with only the reserved
        # ``connector:`` name is not paused here, but the name alone puts it
        # under the connector key scope (#2929, ``connector_scoped`` below,
        # ``auth.connector_key_scope``). A link whose connector is not in the
        # key's workspace (tampered data: the CHECKs, the FK and the mint check
        # rule it out) is refused too, like the agent kill switch. ``None`` —
        # not an exception: the MCP transport turns an exception here into a
        # 503 (#1742). Nothing is stored, so a re-subscription lifts the pause
        # on the next call. Only connector keys pay the one extra SELECT, and a
        # refused attempt is not usage (this runs before the agent liveness
        # touch and the last_used_at write).
        connector_id = key_record.connector_id
        if connector_id is not None:
            from services.plan_suspension import connector_key_workspace, connectors_suspended

            workspace = (
                await connector_key_workspace(self.db, connector_id, key_record.workspace_id)
                if key_record.workspace_id is not None
                else None
            )
            if workspace is None:
                logger.warning(
                    "api_key_rejected_connector_workspace_mismatch",
                    key_prefix=key_record.key_prefix,
                    workspace_id=(
                        str(key_record.workspace_id) if key_record.workspace_id else None
                    ),
                    connector_id=str(connector_id),
                )
                return None
            if connectors_suspended(workspace):
                # ``info``: a paused worker retrying is the expected steady
                # state while the plan has no seats, not an auth anomaly.
                logger.info(
                    "api_key_rejected_connectors_suspended",
                    key_prefix=key_record.key_prefix,
                    workspace_id=str(key_record.workspace_id),
                    connector_id=str(connector_id),
                )
                return None
            # #2918: nor while the workspace is slot-suspended: a connector key
            # reaches no route of a suspended workspace, the owner's
            # management routes included. No statement unless the mode is
            # enforce.
            from services.workspace_slot_gate import slot_suspended

            if await slot_suspended(self.db, key_record.workspace_id):
                logger.info(
                    "api_key_rejected_workspace_slot_suspended",
                    key_prefix=key_record.key_prefix,
                    workspace_id=str(key_record.workspace_id),
                    connector_id=str(connector_id),
                )
                return None

        # Issue #1275 (RFC-0002 P0-2): agent-bound key gates. Only bound keys
        # pay the extra agents SELECT — unbound keys (every credential
        # existing before P0-2) stay byte-for-byte on the pre-#1275 path.
        agent_enforcement_mode: str | None = None
        if key_record.agent_id is not None:
            from models.agent import AGENT_STATUS_ACTIVE, Agent

            agent_result = await self.db.execute(
                select(Agent).where(Agent.id == key_record.agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            # Fail-closed kill switch: a suspended/retired (or somehow
            # missing) agent invalidates every key bound to it — one row
            # update beats revoking N keys.
            if agent is None or agent.status != AGENT_STATUS_ACTIVE:
                logger.warning(
                    "api_key_rejected_agent_kill_switch",
                    key_prefix=key_record.key_prefix,
                    agent_id=str(key_record.agent_id),
                    agent_status=agent.status if agent else "missing",
                )
                return None
            # Defensive re-assert of the mint-time invariant (PostgreSQL
            # cannot express the cross-table workspace equality in a CHECK).
            if agent.workspace_id != key_record.workspace_id:
                logger.warning(
                    "api_key_rejected_agent_workspace_mismatch",
                    key_prefix=key_record.key_prefix,
                    agent_id=str(agent.id),
                )
                return None
            agent_enforcement_mode = agent.enforcement_mode

            # Agent liveness signal — throttled like last_used_at below.
            from services.agent_registry_service import AgentRegistryService

            await AgentRegistryService(self.db).touch_last_seen(agent.id)

        # Update last_used_at — throttled (#947). This runs on EVERY auth (one
        # per MCP tool call), so writing the row each time is hot-row
        # write-amplification. Skip the write while the stored timestamp is
        # fresher than the throttle window; a NULL (never-used) key always
        # writes. The compare is naive-UTC vs naive-UTC (both via utcnow()), and
        # guarding the assignment itself (not just the flush) keeps the session
        # object clean so a later request-level commit cannot persist it anyway.
        now = utcnow()
        throttle = timedelta(seconds=get_settings().api_key_last_used_throttle_seconds)
        if key_record.last_used_at is None or now - key_record.last_used_at >= throttle:
            key_record.last_used_at = now
            await self.db.flush()

        return VerifiedKey(
            id=key_record.id,
            user_id=key_record.user_id,
            workspace_id=key_record.workspace_id,
            bound_context_id=key_record.bound_context_id,
            key_prefix=key_record.key_prefix,
            agent_id=key_record.agent_id,
            agent_enforcement_mode=agent_enforcement_mode,
            is_connector_key=connector_id is not None,
            default_context_id=key_record.default_context_id,
            public_id=key_record.public_id,
            connector_id=connector_id,
            # #2929: the link, or the reserved name alone.
            connector_scoped=(
                connector_id is not None or key_record.name.startswith(CONNECTOR_KEY_PREFIX)
            ),
        )

    async def list_keys(
        self,
        user_id: str | None = None,
        workspace_id: UUID | None = None,
    ) -> list[APIKey]:
        """List all API keys (optionally filtered by user and/or workspace).

        Issue #169: Workspace-scoped API keys.
        Migration 034: Removed context_id filter (deprecated).

        Args:
            user_id: Optional user_id filter
            workspace_id: Optional workspace_id filter (Issue #169)

        Returns:
            List of APIKey entities
        """
        query = select(APIKey).order_by(APIKey.created_at.desc())

        if user_id:
            query = query.where(APIKey.user_id == user_id)

        if workspace_id:
            query = query.where(APIKey.workspace_id == workspace_id)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def revoke_key(self, name: str, user_id: str) -> bool:
        """Revoke an API key.

        Args:
            name: Name of the key to revoke
            user_id: User ID that owns the key

        Returns:
            True if revoked, False if not found
        """
        result = await self.db.execute(
            select(APIKey).where(
                and_(
                    APIKey.name == name,
                    APIKey.user_id == user_id,
                    APIKey.revoked_at.is_(None),
                )
            )
        )
        key_record = result.scalar_one_or_none()

        if not key_record:
            return False

        key_record.revoked_at = utcnow()
        await self.db.flush()

        logger.info("api_key_revoked", name=name, user_id=user_id)

        return True

    async def delete_key(self, name: str, user_id: str) -> bool:
        """Permanently delete an API key.

        Args:
            name: Name of the key to delete
            user_id: User ID that owns the key

        Returns:
            True if deleted, False if not found
        """
        result = await self.db.execute(
            select(APIKey).where(and_(APIKey.name == name, APIKey.user_id == user_id))
        )
        key_record = result.scalar_one_or_none()

        if not key_record:
            return False

        await self.db.delete(key_record)
        await self.db.flush()

        logger.info("api_key_deleted", name=name, user_id=user_id)

        return True

    async def hide_key(self, key_id: int, user_id: str) -> None:
        """Manually hide API key (owner verification).

        Migration 034: Zero-knowledge model - only owner can hide.

        Args:
            key_id: ID of the key to hide
            user_id: User ID (owner verification)

        Raises:
            ValueError: If key not found
            PermissionError: If user is not the owner
        """
        result = await self.db.execute(select(APIKey).where(APIKey.id == key_id))
        key = result.scalar_one_or_none()

        if not key:
            raise ValueError(f"API key with ID {key_id} not found")

        # Zero-knowledge: only owner can hide
        if key.user_id != user_id:
            raise PermissionError("Only owner can hide API key")

        apply_zero_knowledge_hide(key)
        await self.db.flush()

        logger.info("api_key_hidden", key_id=key_id, public_id=key.public_id, user_id=user_id)

    @staticmethod
    def should_auto_hide(key: APIKey) -> bool:
        """Check if key should be auto-hidden.

        Migration 034: Auto-hide logic for background job.

        Args:
            key: APIKey instance

        Returns:
            True if key should be auto-hidden
        """
        # Already hidden
        if key.hidden_at:
            return False

        # No expiration set
        if not key.visibility_expires_at:
            return False

        # Check if expired
        return utcnow() >= key.visibility_expires_at
