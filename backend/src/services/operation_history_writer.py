"""Add allowlisted mutation evidence to the caller's transaction (#2739).

Never commits independently or checks a subscription: recording runs on all
tiers. The paid feature is reading. A rollback discards both change and event.
"""

from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import event, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.auth import Context
from models.operation_history import OperationHistory
from services.write_provenance import current_provenance
from utils.datetime import utcnow


@event.listens_for(Session, "before_flush")
def _insert_history_once(session: Session, _flush_context: Any, _instances: Any) -> None:
    """Persist queued evidence in the caller's transaction, including replays.

    Revision recording is synchronous. Keep its queue API while PostgreSQL
    arbitrates concurrent source keys without aborting the business mutation.
    Context-ID-only callers must reference a persisted context; creation paths
    flush their context first or supply a concrete privacy snapshot. Returned
    evidence objects are detached after flush and must not be refreshed.
    """
    for pending in list(session.new):
        if not isinstance(pending, OperationHistory):
            continue
        values = {
            column.name: getattr(pending, column.name)
            for column in OperationHistory.__table__.columns
        }
        session.expunge(pending)
        session.connection(bind_arguments={"mapper": OperationHistory}).execute(
            insert(OperationHistory.__table__)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["id"])
        )


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
        "credential",
        "pinned",
        "expires_at",
        "event_start",
        "event_end",
        "delivery_mode",
    }
)


def record_operation(
    db: AsyncSession,
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
    agent_id: UUID | None = None,
    private_before: bool = False,
) -> OperationHistory | None:
    """Queue one event; only field NAMES from the closed vocabulary survive.

    For the synchronous revision writer, scalar subqueries capture the context
    boundary in the INSERT, without an extra asynchronous lookup. Callers that
    delete/detach a context pass its pre-change snapshot instead. Event keys
    identify a committed source operation, never a client-supplied label.
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
    source = surface or ("mcp" if corr and corr.surface == "mcp" else "api" if corr else "unknown")
    if source not in {"web", "api", "mcp", "system", "unknown"}:
        raise ValueError("Unsupported operation history surface")
    identifier = uuid5(NAMESPACE_URL, "kmc-operation:" + event_key) if event_key else uuid4()
    # A caller may queue the same source twice before flushing. The PK also
    # prevents a source from acquiring two persisted representations.
    for pending in db.new:
        if isinstance(pending, OperationHistory) and pending.id == identifier:
            return pending
    values: dict[str, Any] = {
        "id": identifier,
        "workspace_id": workspace_id,
        "context_id": context_id,
        "context_private": False,
        "context_owner_id": None,
        "occurred_at": at or utcnow(),
        "actor_user_id": actor_user_id,
        "subject_user_id": subject_user_id,
        "agent_id": agent_id or (current_provenance().agent_id if source != "system" else None),
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
    event = OperationHistory(**values)
    db.add(event)
    return event
