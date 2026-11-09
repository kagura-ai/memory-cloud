"""Permission-scoped operation history queries (#2739)."""

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, User
from models.operation_history import OperationHistory as Event
from services.identity_link_service import linked_user_ids
from utils.datetime import to_utc_iso
from utils.exceptions import ValidationError


def encode_cursor(at: datetime, event_id: UUID) -> str:
    return base64.urlsafe_b64encode(json.dumps([to_utc_iso(at), str(event_id)]).encode()).decode()


def decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        if len(value) > 512:
            raise ValueError("length")
        decoded = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or not all(isinstance(item, str) for item in decoded)
        ):
            raise ValueError("cursor shape")
        at, event_id = decoded
        timestamp = datetime.fromisoformat(at)
        if timestamp.tzinfo is None:
            raise ValueError("timezone")
        return timestamp.astimezone(UTC), UUID(event_id)
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise ValidationError("Invalid operation history cursor", field="cursor") from exc


def visibility_conditions(workspace_id: UUID, owner_ids: frozenset[str]) -> list:
    """Apply historical AND present privacy before rows, counts or search.

    A purged context is restricted to its recorded creator; a moved context must
    still belong here. No role, even workspace owner, opens another owner's
    private context. A later public transition does not reveal old private events.
    """
    current_context = exists(select(Context.id).where(Context.id == Event.context_id))
    currently_visible = exists(
        select(Context.id).where(
            Context.id == Event.context_id,
            Context.workspace_id == workspace_id,
            or_(Context.is_private.is_(False), Context.created_by.in_(owner_ids)),
        )
    )
    return [
        Event.workspace_id == workspace_id,
        or_(Event.context_private.is_(False), Event.context_owner_id.in_(owner_ids)),
        or_(
            Event.context_id.is_(None),
            and_(~current_context, Event.context_owner_id.in_(owner_ids)),
            currently_visible,
        ),
    ]


async def read_history(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: str,
    limit: int,
    query: str = "",
    context_id: UUID | None = None,
    actor: str | None = None,
    agent_id: UUID | None = None,
    operation: str | None = None,
    target_type: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
) -> dict:
    owners = await linked_user_ids(db, user_id)
    conditions = visibility_conditions(workspace_id, owners)
    for column, value in (
        (Event.context_id, context_id),
        (Event.actor_user_id, actor),
        (Event.agent_id, agent_id),
        (Event.operation, operation),
        (Event.target_type, target_type),
        (Event.outcome, outcome),
    ):
        if value is not None:
            conditions.append(column == value)
    if since:
        conditions.append(Event.occurred_at >= since)
    if until:
        conditions.append(Event.occurred_at <= until)
    if query.strip():
        text = query.strip()
        conditions.append(
            or_(
                *[
                    cast(column, String).icontains(text, autoescape=True)
                    for column in (
                        Event.actor_user_id,
                        Event.subject_user_id,
                        Event.agent_id,
                        Event.target_id,
                        Event.context_id,
                        Event.operation,
                        Event.target_type,
                        Event.changed_fields,
                        User.name,
                    )
                ]
            )
        )
    # The actor's current display name is resolved live, never copied into history.
    joined = Event.__table__.outerjoin(User, User.user_id == Event.actor_user_id)
    total = await db.scalar(select(func.count()).select_from(joined).where(*conditions))
    if cursor:
        at, identifier = decode_cursor(cursor)
        conditions.append(tuple_(Event.occurred_at, Event.id) < tuple_(at, identifier))
    rows = (
        await db.execute(
            select(Event, User.name.label("actor_name"))
            .select_from(joined)
            .where(and_(*conditions))
            .order_by(Event.occurred_at.desc(), Event.id.desc())
            .limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    items = [
        {
            "id": event.id,
            "occurred_at": event.occurred_at,
            "context_id": event.context_id,
            "actor_user_id": event.actor_user_id,
            "actor_name": name,
            "subject_user_id": event.subject_user_id,
            "agent_id": event.agent_id,
            "surface": event.surface,
            "operation": event.operation,
            "target_type": event.target_type,
            "target_id": event.target_id,
            "outcome": event.outcome,
            "count": event.count,
            "changed_fields": event.changed_fields,
        }
        for event, name in page
    ]
    return {
        "items": items,
        "total": total or 0,
        "next_cursor": encode_cursor(page[-1][0].occurred_at, page[-1][0].id)
        if len(rows) > limit
        else None,
    }
