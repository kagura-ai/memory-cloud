"""Agent session-state lane service (Issue #889).

CRUD over the dedicated ``agent_states`` table — a TTL-bounded key/value store
for ephemeral agent run-state, structurally excluded from ``recall()`` (it lives
in its own table, not ``memories``, and is never embedded).

Reads apply a **lazy TTL filter**: expired rows (``expires_at <= now``) are
never returned, and a best-effort opportunistic delete sweeps them. ``set_state``
upserts on ``(context_id, key)`` via PostgreSQL ``ON CONFLICT``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.agent_state import AgentState
from utils.datetime import utcnow

# Guardrail so a caller cannot pin an unbounded TTL far in the future; the lane
# is for ephemeral run-state, not long-lived storage. 30 days is generous.
MAX_TTL_SECONDS = 30 * 24 * 60 * 60


class AgentStateService:
    """Set / get / list / delete agent run-state entries, scoped to a context."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def set_state(
        self,
        context_id: UUID,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        """Upsert ``value`` at ``(context_id, key)``.

        ``ttl_seconds`` (when given, clamped to ``MAX_TTL_SECONDS``) sets
        ``expires_at = now + ttl``; omitting it clears any prior TTL so the
        entry persists until explicitly deleted.
        """
        expires_at = None
        if ttl_seconds is not None and ttl_seconds > 0:
            expires_at = utcnow() + timedelta(seconds=min(ttl_seconds, MAX_TTL_SECONDS))

        stmt = pg_insert(AgentState).values(
            context_id=context_id,
            key=key,
            value=value,
            expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["context_id", "key"],
            set_={
                "value": value,
                "expires_at": expires_at,
                "updated_at": utcnow(),
            },
        )
        await self.db.execute(stmt)
        await self.db.commit()

    async def get_state(self, context_id: UUID, key: str) -> Any | None:
        """Return the live value at ``(context_id, key)``, or ``None``.

        Expired entries are treated as absent (and swept opportunistically).
        """
        now = utcnow()
        row = (
            await self.db.execute(
                select(AgentState).where(
                    and_(
                        AgentState.context_id == context_id,
                        AgentState.key == key,
                        or_(
                            AgentState.expires_at.is_(None),
                            AgentState.expires_at > now,
                        ),
                    )
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # Either truly absent or expired — sweep an expired tombstone so the
            # table doesn't accumulate dead keys on a hot read path.
            await self._reap_expired(context_id, key)
            return None
        return row.value

    async def list_state(self, context_id: UUID) -> dict[str, Any]:
        """Return all live ``key → value`` entries for the context.

        Opportunistically reaps the context's expired rows first so a
        list-heavy read pattern (agents reading all state each step) still
        drives the TTL sweep and the table doesn't accumulate dead rows.
        """
        await self._reap_expired_context(context_id)
        now = utcnow()
        rows = (
            await self.db.execute(
                select(AgentState.key, AgentState.value).where(
                    and_(
                        AgentState.context_id == context_id,
                        or_(
                            AgentState.expires_at.is_(None),
                            AgentState.expires_at > now,
                        ),
                    )
                )
            )
        ).all()
        return {row.key: row.value for row in rows}

    async def list_state_detail(self, context_id: UUID) -> list[dict[str, Any]]:
        """Return live entries for the context WITH per-entry recency (#1064).

        Like ``list_state`` but each item carries its own ``updated_at`` so a
        read-only observation view (e.g. an external session-status dashboard
        driven by a #1027 share key) can render staleness honestly — the
        consumer treats this timestamp as the *historical* checkpoint time, not
        "live now". Same lazy-reap + live filter as ``list_state``.

        Returns a list of ``{"key", "value", "updated_at"}`` dicts, ordered by
        recency (most recently updated first).
        """
        await self._reap_expired_context(context_id)
        now = utcnow()
        rows = (
            await self.db.execute(
                select(
                    AgentState.key,
                    AgentState.value,
                    AgentState.updated_at,
                )
                .where(
                    and_(
                        AgentState.context_id == context_id,
                        or_(
                            AgentState.expires_at.is_(None),
                            AgentState.expires_at > now,
                        ),
                    )
                )
                .order_by(AgentState.updated_at.desc())
            )
        ).all()
        return [{"key": row.key, "value": row.value, "updated_at": row.updated_at} for row in rows]

    async def delete_state(self, context_id: UUID, key: str) -> bool:
        """Delete ``(context_id, key)``. Returns True if a row was removed."""
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(AgentState).where(
                    and_(
                        AgentState.context_id == context_id,
                        AgentState.key == key,
                    )
                )
            ),
        )
        await self.db.commit()
        return result.rowcount > 0

    async def _reap_expired(self, context_id: UUID, key: str) -> None:
        """Best-effort delete of a single expired tombstone (lazy sweep)."""
        now = utcnow()
        await self.db.execute(
            delete(AgentState).where(
                and_(
                    AgentState.context_id == context_id,
                    AgentState.key == key,
                    AgentState.expires_at.is_not(None),
                    AgentState.expires_at <= now,
                )
            )
        )
        await self.db.commit()

    async def _reap_expired_context(self, context_id: UUID) -> None:
        """Best-effort delete of ALL expired rows for a context (list sweep)."""
        now = utcnow()
        await self.db.execute(
            delete(AgentState).where(
                and_(
                    AgentState.context_id == context_id,
                    AgentState.expires_at.is_not(None),
                    AgentState.expires_at <= now,
                )
            )
        )
        await self.db.commit()
