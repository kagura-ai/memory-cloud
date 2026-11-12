"""Add allowlisted mutation evidence to the caller's transaction (#2739).

Recording is limited to workspaces entitled to ``operation_history`` at write
time: the INSERT itself checks the workspace's plan, so an event of a workspace
on another tier is simply not stored. Never commits independently. A rollback
discards both change and event.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import cast, event, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from auth.history_principal import HistoryPrincipal, get_history_principal
from auth.system_admin_mode import OPERATOR_PRINCIPAL_PREFIX
from config.plan_tiers import plans_with_feature
from models.auth import Context, Workspace
from models.operation_history import OperationHistory
from services.write_provenance import current_provenance

_UNSET_AGENT = object()
FEATURE = "operation_history"
# Events queued in a session and not flushed yet, by event id (``Session.info``).
_QUEUE_KEY = "operation_history_queue"


def _queue(db: AsyncSession | Session) -> dict[UUID, OperationHistory]:
    """The session's not-yet-flushed events. A session's ``info`` is always a
    dict; anything else (a test double) just goes without deduplication."""
    info = db.info
    return info.setdefault(_QUEUE_KEY, {}) if isinstance(info, dict) else {}


def _bound(column: Any, value: Any) -> Any:
    """``value`` as a typed SELECT-list expression for ``INSERT ... SELECT``."""
    if isinstance(value, ColumnElement) or hasattr(value, "__clause_element__"):
        return value
    return cast(literal(value, type_=column.type), column.type)


@event.listens_for(Session, "before_flush")
def _insert_history_once(session: Session, _flush_context: Any, _instances: Any) -> None:
    """Persist queued evidence in the caller's transaction, including replays.

    Revision recording is synchronous, so it keeps a queue API; the INSERT is
    issued here in the same transaction as the business write. ``ON CONFLICT
    DO NOTHING`` on the event id makes a replayed or concurrently delivered
    source key a no-op. Any other failure of this INSERT fails the flush and
    so aborts the business mutation with it: a covered change never commits
    without its evidence (intentional coupling, no best-effort savepoint).

    The INSERT is ``INSERT ... SELECT ... WHERE EXISTS``: it stores the event
    only while its workspace is on a tier entitled to ``operation_history``
    (registry tiers, ``PLAN_<KEY>_FEATURES`` overrides included). Context-ID-only
    callers must reference a persisted context; creation paths flush their
    context first or supply a concrete privacy snapshot. Returned evidence
    objects are detached after flush and must not be refreshed.
    """
    session.info.pop(_QUEUE_KEY, None)
    pending = [obj for obj in session.new if isinstance(obj, OperationHistory)]
    if not pending:
        return
    plans = plans_with_feature(FEATURE)
    table = OperationHistory.__table__
    for row in pending:
        values = {column.name: getattr(row, column.name) for column in table.columns}
        session.expunge(row)
        if not plans:
            continue
        entitled = exists().where(
            Workspace.id == values["workspace_id"], Workspace.plan_name.in_(plans)
        )
        source = select(*(_bound(column, values[column.name]) for column in table.columns))
        session.connection(bind_arguments={"mapper": OperationHistory}).execute(
            insert(table)
            .from_select([column.name for column in table.columns], source.where(entitled))
            .on_conflict_do_nothing(index_elements=["id"])
        )


@event.listens_for(Session, "after_soft_rollback")
def _forget_rolled_back_events(session: Session, _previous_transaction: Any) -> None:
    """A rollback (savepoints included) expunges the events queued inside it,
    so a retry of the same source must queue a fresh one."""
    session.info.pop(_QUEUE_KEY, None)


OPERATIONS = frozenset(
    {
        "memory.created",
        "memory.updated",
        "memory.deleted",
        "memory.restored",
        "context.created",
        "context.updated",
        "context.deleted",
        "context.restored",
        "member.invited",
        "member.added",
        "member.removed",
        "member.role_changed",
        "member.access_changed",
        "credential.issued",
        "credential.updated",
        "credential.revoked",
        "integration.created",
        "integration.updated",
        "integration.deleted",
        "sleep.completed",
        "sleep.failed",
        "sleep.undo",
    }
)
SAFE_FIELDS = frozenset(
    {
        "content",
        "summary",
        "details",
        "tags",
        "type",
        "importance",
        "scope",
        "is_private",
        "is_public",
        "is_locked",
        "name",
        "display_name",
        "description",
        "usage_guide",
        "resource_id",
        "sleep_mode",
        "role",
        "allowed_context_ids",
        "runtime",
        "settings",
        # #1759: a connector's per-channel destinations or its unrouted-private
        # switch changed (field name only: never a channel or context name).
        "channel_routes",
        "credential",
        "pinned",
        "expires_at",
        "event_start",
        "event_end",
        "delivery_mode",
        "redirect_uris",
        "token_endpoint_auth_method",
    }
)


def record_operation(
    db: AsyncSession | Session,
    *,
    operation: str,
    actor_user_id: str | None,
    workspace_id: UUID | None = None,
    context_id: UUID | None = None,
    context: Context | None = None,
    target_id: str | UUID | None = None,
    subject_user_id: str | None = None,
    count: int = 1,
    changed_fields: list[str] | None = None,
    outcome: str = "success",
    surface: str | None = None,
    at: datetime | None = None,
    event_key: str | None = None,
    agent_id: UUID | None | object = _UNSET_AGENT,
    private_before: bool = False,
) -> OperationHistory | None:
    """Queue one event; only field NAMES from the closed vocabulary survive.

    For the synchronous revision writer, scalar subqueries capture the context
    boundary in the INSERT, without an extra asynchronous lookup. Callers that
    delete/detach a context pass its pre-change snapshot instead. Event keys
    identify a committed source operation, never a client-supplied label.
    Whether the event is stored at all is decided at flush time by the
    workspace's entitlement (see ``_insert_history_once``). Works on an
    ``AsyncSession`` and on the synchronous OAuth2 ``Session`` alike.
    """
    if operation not in OPERATIONS or outcome not in {"success", "failure", "denied"}:
        raise ValueError("Unsupported operation history vocabulary")
    if count < 1:
        raise ValueError("Operation count must be positive")
    if context is not None:
        context_id = context.id
        workspace_id = context.workspace_id
    if workspace_id is None and context_id is None:
        return None  # Legacy unscoped data cannot be assigned to a workspace.
    from api.correlation import get_correlation

    corr = get_correlation()
    principal = get_history_principal()
    source = surface or (
        "system"
        if principal.kind == "system"
        else "mcp"
        if corr and corr.surface == "mcp"
        else "web"
        if principal.kind == "session"
        else "api"
        if principal.kind not in {"unknown", "system"}
        else "unknown"
    )
    if source not in {"web", "api", "mcp", "system", "unknown"}:
        raise ValueError("Unsupported operation history surface")
    if source == "system":
        principal = HistoryPrincipal("system")
        actor_user_id = None
    if principal.kind == "operator" or (actor_user_id or "").startswith(OPERATOR_PRINCIPAL_PREFIX):
        # #2767: a service operator is staff, not a workspace member. Its
        # address never enters this customer-visible table (the read API
        # returns and filters on actor_user_id); accountability stays in the
        # audit_logs row the same operation writes, as for the system surface.
        principal = HistoryPrincipal("operator")
        actor_user_id = None
    occurred_at = at or datetime.now(UTC)
    # Existing source records use naive UTC. Never let the process timezone
    # reinterpret those values while binding to this TIMESTAMPTZ column.
    occurred_at = (
        occurred_at.replace(tzinfo=UTC)
        if occurred_at.tzinfo is None
        else occurred_at.astimezone(UTC)
    )
    identifier = uuid5(NAMESPACE_URL, "kmc-operation:" + event_key) if event_key else uuid4()
    # A caller may queue the same source twice before flushing. The PK also
    # prevents a source from acquiring two persisted representations. A dict
    # lookup, not a scan of ``session.new``: a bulk path queues thousands.
    queue = _queue(db)
    if (queued := queue.get(identifier)) is not None:
        return queued
    values: dict[str, Any] = {
        "id": identifier,
        "workspace_id": workspace_id,
        "context_id": context_id,
        "context_private": False,
        "context_owner_id": None,
        "occurred_at": occurred_at,
        "actor_user_id": actor_user_id,
        "subject_user_id": subject_user_id,
        "agent_id": None
        if source == "system"
        else (current_provenance().agent_id if agent_id is _UNSET_AGENT else agent_id),
        "principal_kind": principal.kind,
        "credential_id": principal.credential_id,
        "client_id": principal.client_id,
        "surface": source,
        "operation": operation,
        "target_type": operation.split(".")[0],
        "target_id": str(target_id) if target_id is not None else None,
        "outcome": outcome,
        "count": count,
        "changed_fields": sorted(SAFE_FIELDS.intersection(changed_fields or [])),
    }
    if context is not None:
        values.update(
            context_private=bool(context.is_private or private_before),
            context_owner_id=context.created_by,
        )
    elif context_id is not None:
        predicate = Context.id == context_id
        if workspace_id is None:
            values["workspace_id"] = select(Context.workspace_id).where(predicate).scalar_subquery()
        values["context_private"] = func.coalesce(
            select(Context.is_private).where(predicate).scalar_subquery(), True
        )
        values["context_owner_id"] = select(Context.created_by).where(predicate).scalar_subquery()
    evidence = OperationHistory(**values)
    queue[identifier] = evidence
    db.add(evidence)
    return evidence
