"""One-call session start for an interactive user credential (#1851).

``get_agent_bootstrap`` rehydrates an *agent*; the session-start skill of an
interactive client has no agent and used to make seven calls. ``bootstrap``
composes the deterministic session-start reads into one bounded envelope by
delegating to the same chokepoints the standalone tools use — pinned
(``MemoryService.load_pinned``), upcoming (``query_upcoming_time_memories``),
changes (``memory_listing.changes_since``) and the context block shared with
``get_agent_bootstrap``. No recall component: the probabilistic reads stay
out of the bootstrap on purpose; the skill recalls by topic afterwards if the
change list does not answer its question.

Components are fail-soft: a failing one reports ``{status: error}`` while the
rest return, with top-level ``degraded: true`` — the same contract as the
agent bootstrap. The guardrails block is computed by the handler (it lives in
the tools layer) and passed in, so this module never imports ``mcp_server``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from services.agent_bootstrap_service import (
    context_and_instructions,
    fail_soft_component,
    pinned_item,
)
from services.memory_listing import CHANGE_CURSOR_RESERVE
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger
from utils.response_budget import fit_lanes, json_chars

logger = get_logger(__name__)

COMPONENTS: tuple[str, ...] = ("pinned", "upcoming", "changes")
PINNED_CAP = 20
UPCOMING_K = 20
CHANGES_LIMIT = 50
STATUS_OK = "ok"
STATUS_ERROR = "error"
# Lane priority when the envelope has to be cut: standing guardrails first,
# then what is due, then what changed.
_LANES = (("pinned", "memories"), ("upcoming", "results"), ("changes", "changes"))


async def _component(db: AsyncSession, name: str, fn: Any) -> dict[str, Any]:
    """One lane, fail-soft: the agent bootstrap's boundary with this tool's error code."""
    return await fail_soft_component(db, name, fn, error_code="component_failed")


class SessionBootstrapService:
    """Compose the session-start envelope for ``user_id`` on ``context``."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def build(
        self,
        *,
        user_id: str,
        workspace_id: UUID | None,
        context: Any,
        since: datetime,
        include: tuple[str, ...] = COMPONENTS,
        max_chars: int,
        guardrails_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Return the bounded envelope.

        ``since`` is naive UTC. ``guardrails_provider`` returns the
        ``get_context_info`` guardrails block (``{"guardrails": ...}``, or
        ``{}`` when the URL switched the lane off); it runs after the context
        block is read and before the lanes, because its fail-open rollback —
        like a lane's — expires every ORM instance in the session.
        """
        context_block, instructions = await context_and_instructions(self.db, context)
        # Scalars only from here on: a rollback (the guardrails read's fail-open
        # one, or a lane's) expires every ORM instance in the session, and
        # touching ``context.id`` afterwards would be a lazy load outside the
        # async greenlet (``MissingGreenlet``). The handler captured its own
        # context fields before calling us.
        context_id: UUID = context.id
        is_private: bool = bool(context.is_private)
        guardrails: dict[str, Any] = {}
        if guardrails_provider is not None:
            try:
                guardrails = await guardrails_provider()
            except Exception as exc:  # the block is advisory; the session start is not
                logger.warning("session_bootstrap_guardrails_failed", exc_info=exc)
                guardrails = {"guardrails": None}
        components: dict[str, Any] = {}
        change_cursors: list[str] = []
        if "pinned" in include:
            components["pinned"] = await _component(
                self.db, "pinned", lambda: self._pinned(user_id, workspace_id, context_id)
            )
        if "upcoming" in include:
            components["upcoming"] = await _component(
                self.db, "upcoming", lambda: self._upcoming(context_id)
            )
        if "changes" in include:
            components["changes"] = await _component(
                self.db, "changes", lambda: self._changes(user_id, context_id, is_private, since)
            )
            # Per-item continuation tokens, kept out of the envelope: a cut
            # page points its next_cursor at the last change it kept.
            change_cursors = components["changes"].pop("_cursors", [])
        envelope: dict[str, Any] = {
            "status": "success",
            "degraded": any(c.get("status") == STATUS_ERROR for c in components.values()),
            "context": context_block,
            "instructions": instructions,
            **guardrails,
            "components": components,
            "since": to_utc_iso(since),
            "generated_at": to_utc_iso(utcnow()),
        }
        return _fit(envelope, max_chars, change_cursors=change_cursors, since=since)

    async def _pinned(
        self, user_id: str, workspace_id: UUID | None, context_id: UUID
    ) -> dict[str, Any]:
        from services.memory_service import MemoryService

        result = await MemoryService(self.db).load_pinned(
            user_id=user_id,
            current_context_id=context_id,
            current_workspace_id=workspace_id,
            cap=PINNED_CAP,
            trusted_only=True,  # behaviour-establishing lane, like the agent bootstrap
        )
        return {
            "memories": [pinned_item(m) for m in result.memories],
            "total_available": result.total_available,
            "truncated": result.truncated,
            "cap": result.cap,
        }

    async def _upcoming(self, context_id: UUID) -> dict[str, Any]:
        from services.time_memory import query_upcoming_time_memories
        from utils.time_trigger import parse_query_bound

        q_from = parse_query_bound("now")
        results = await query_upcoming_time_memories(
            self.db, context_id, q_from=q_from, q_until=None, k=UPCOMING_K, trusted_only=True
        )
        return {"results": results, "from": q_from}

    async def _changes(
        self, user_id: str, context_id: UUID, is_private: bool, since: datetime
    ) -> dict[str, Any]:
        from services.memory_listing import KINDS, change_item, changes_since, encode_change_cursor

        page = await changes_since(
            self.db,
            context_id=context_id,
            owner_user_id=user_id if is_private else None,
            since=since,
            until=None,
            kinds=KINDS,
            limit=CHANGES_LIMIT,
            trusted_only=True,
        )
        return {
            "changes": [change_item(c) for c in page.changes],
            "has_more": page.next_cursor is not None,
            "next_cursor": page.next_cursor,
            "_cursors": [encode_change_cursor(c) for c in page.changes],
        }


def _fit(
    envelope: dict[str, Any],
    max_chars: int,
    *,
    change_cursors: list[str],
    since: datetime,
) -> dict[str, Any]:
    """Hold the envelope to ``max_chars``: ``context_summary`` leaves the pinned
    items first, then each lane keeps the prefix that fits (``truncated`` per
    component). Never silent: a cut ``changes`` page points ``next_cursor`` at
    the last change it kept (``change_cursors``, one per item in page order),
    or at the start of the window when nothing was kept, so paging on from the
    reply reproduces the cut items instead of skipping them."""
    if json_chars(envelope) <= max_chars:
        return envelope
    components = envelope["components"]
    present = [
        (name, key)
        for name, key in _LANES
        if components.get(name, {}).get("status") == STATUS_OK
        and isinstance(components[name].get(key), list)
    ]
    shell = {
        **envelope,
        "components": {
            **components,
            **{name: {**components[name], key: [], "truncated": True} for name, key in present},
        },
    }
    if "changes" in dict(present):
        # A cut changes page gets a keyset next_cursor afterwards: reserve its room.
        shell["components"]["changes"]["next_cursor"] = "x" * CHANGE_CURSOR_RESERVE
    budget = max_chars - json_chars(shell) - len('"context_summary_omitted":true,')
    bounded, cut, dropped = fit_lanes(
        [components[name][key] for name, key in present], budget, droppable="context_summary"
    )
    new_components = dict(components)
    for (name, key), items, was_cut in zip(present, bounded, cut, strict=True):
        component = {**components[name], key: items}
        if was_cut:
            component["truncated"] = True
            if name == "changes":
                component["has_more"] = True
                kept = len(items)
                component["next_cursor"] = (
                    change_cursors[kept - 1] if kept else window_start_cursor(since)
                )
        new_components[name] = component
    out = {**envelope, "components": new_components}
    if dropped:
        out["context_summary_omitted"] = True
    return out


def window_start_cursor(since: datetime) -> str:
    """A ``changes_since`` cursor that sorts before every event at or after ``since``.

    ``created`` is the alphabetically first kind and the nil UUID the smallest
    id, so the keyset ``(at, kind, id) > (since, "created", nil)`` admits every
    event in ``[since, …)`` except a ``created`` event of the nil UUID at exactly
    ``since`` — which cannot exist. Used when a budget cut kept no change.
    """
    from services.memory_listing import Change, encode_change_cursor

    return encode_change_cursor(
        Change(memory_id=UUID(int=0), kind="created", at=since, summary="", superseded_by=None)
    )
