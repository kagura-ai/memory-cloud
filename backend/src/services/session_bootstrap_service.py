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

``format="index"`` (#2126) turns the same reads into pointer rows: pinned
keeps its text, guardrails / upcoming / changes rows carry an 80-character
summary head (``summary_cut``), ``instructions`` is left out, and the
guardrails items become a lane the budget may cut.

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
from utils.recall_projection import INDEX_SUMMARY_CHARS, compact_summary
from utils.response_budget import fit_lanes, json_chars

logger = get_logger(__name__)

COMPONENTS: tuple[str, ...] = ("pinned", "upcoming", "changes")
FORMAT_FULL = "full"
FORMAT_INDEX = "index"
FORMATS: tuple[str, ...] = (FORMAT_FULL, FORMAT_INDEX)
PINNED_CAP = 20
UPCOMING_K = 20
CHANGES_LIMIT = 50
STATUS_OK = "ok"
STATUS_ERROR = "error"
# Lane priority when the envelope has to be cut: standing guardrails first,
# then what is due, then what changed.
_LANES = (("pinned", "memories"), ("upcoming", "results"), ("changes", "changes"))
# #2126: in index mode the guardrails items are a lane too, right after pinned.
# Full mode keeps them uncut (the get_context_info contract).
_GUARDRAILS = "guardrails"
_INDEX_LANES = (_LANES[0], (_GUARDRAILS, "items"), *_LANES[1:])


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
        format: str = FORMAT_FULL,
    ) -> dict[str, Any]:
        """Return the bounded envelope.

        ``since`` is naive UTC. ``guardrails_provider`` returns the
        ``get_context_info`` guardrails block (``{"guardrails": ...}``, or
        ``{}`` when the URL switched the lane off); it runs after the context
        block is read and before the lanes, because its fail-open rollback —
        like a lane's — expires every ORM instance in the session.

        ``format`` is ``"full"`` (the default shape) or ``"index"`` (pointer
        rows, ``format: "index"`` in the reply, no ``instructions``); the
        handler resolves it from the arguments.
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
        degraded = any(c.get("status") == STATUS_ERROR for c in components.values())
        if format == FORMAT_INDEX:
            index_envelope: dict[str, Any] = {
                "status": "success",
                "format": FORMAT_INDEX,
                "degraded": degraded,
                "context": context_block,
                **{k: index_guardrails(v) for k, v in guardrails.items()},
                "components": index_components(components),
                "since": to_utc_iso(since),
                "generated_at": to_utc_iso(utcnow()),
            }
            return _fit(
                index_envelope, max_chars, change_cursors=change_cursors, since=since, index=True
            )
        envelope: dict[str, Any] = {
            "status": "success",
            "degraded": degraded,
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


def _summary_head(summary: str) -> dict[str, Any]:
    """``summary`` cut to an index row's head, plus ``summary_cut`` when text was dropped."""
    head, cut = compact_summary(summary, INDEX_SUMMARY_CHARS)
    return {"summary": head, "summary_cut": True} if cut else {"summary": head}


def index_pinned_row(item: dict[str, Any]) -> dict[str, Any]:
    """A pinned row keeps its full text; ``context_summary`` and the constant
    ``delivery_mode`` stay in the full shape (``load_pinned`` has them)."""
    return {k: item[k] for k in ("memory_id", "summary", "type", "importance") if k in item}


def index_guardrail_row(item: dict[str, Any]) -> dict[str, Any]:
    """``{memory_id, summary, summary_cut?, importance}`` — the hook or
    ``get_context_info`` delivers the whole lesson."""
    summary = item.get("summary")
    return {
        "memory_id": item.get("memory_id"),
        **(_summary_head(summary) if isinstance(summary, str) else {}),
        "importance": item.get("importance"),
    }


def index_upcoming_row(item: dict[str, Any]) -> dict[str, Any]:
    """``{memory_id, summary, summary_cut?, from, until}`` — the trigger's window
    (``until`` too: a month-wide window drawn from ``from`` alone reads as one day)."""
    summary = item.get("summary")
    trigger = item.get("trigger")
    window = trigger if isinstance(trigger, dict) else {}
    return {
        "memory_id": item.get("memory_id"),
        **(_summary_head(summary) if isinstance(summary, str) else {}),
        **{k: window[k] for k in ("from", "until") if window.get(k) is not None},
    }


def index_change_row(item: dict[str, Any]) -> dict[str, Any]:
    """``change_item`` with the summary cut; a forgotten memory's event has none.

    ``summary`` sits before ``superseded_by`` in ``change_item``, and the
    inserted ``summary_cut`` lands right after it.
    """
    summary = item.get("summary")
    if not isinstance(summary, str):
        return item
    head = _summary_head(summary)
    return {
        **{k: v for k, v in item.items() if k != "superseded_by"},
        **head,
        **({"superseded_by": item["superseded_by"]} if "superseded_by" in item else {}),
    }


_INDEX_ROWS = {
    "pinned": ("memories", index_pinned_row),
    "upcoming": ("results", index_upcoming_row),
    "changes": ("changes", index_change_row),
}


def index_components(components: dict[str, Any]) -> dict[str, Any]:
    """Each healthy lane's rows as index rows; a failed lane passes through."""
    out: dict[str, Any] = {}
    for name, component in components.items():
        key, row = _INDEX_ROWS[name]
        if component.get("status") == STATUS_OK and isinstance(component.get(key), list):
            component = {**component, key: [row(item) for item in component[key]]}
        out[name] = component
    return out


def index_guardrails(block: Any) -> Any:
    """The provider's guardrails block with its items as index rows.

    ``None`` (the read failed) and anything without an ``items`` list pass
    through unchanged; the block keeps ``provenance``, ``total_available``,
    ``truncated`` and ``tool_triggered_version``.
    """
    if not isinstance(block, dict) or not isinstance(block.get("items"), list):
        return block
    return {**block, "items": [index_guardrail_row(i) for i in block["items"]]}


def _fit(
    envelope: dict[str, Any],
    max_chars: int,
    *,
    change_cursors: list[str],
    since: datetime,
    index: bool = False,
) -> dict[str, Any]:
    """Hold the envelope to ``max_chars``: ``context_summary`` leaves the pinned
    items first, then each lane keeps the prefix that fits (``truncated`` per
    component). Never silent: a cut ``changes`` page points ``next_cursor`` at
    the last change it kept (``change_cursors``, one per item in page order),
    or at the start of the window when nothing was kept, so paging on from the
    reply reproduces the cut items instead of skipping them.

    ``index`` (#2126) adds ``guardrails.items`` as the second lane (its block's
    own ``truncated`` turns true when cut); index rows carry no
    ``context_summary``, so nothing is dropped first. When the uncuttable
    shell alone exceeds ``max_chars``, every lane comes back empty with
    ``truncated: true`` — the reply may then exceed ``max_chars`` but says so."""
    if json_chars(envelope) <= max_chars:
        return envelope
    components = envelope["components"]

    def container(name: str) -> dict[str, Any]:
        """The dict holding a lane's list (``{}`` when absent, ``null`` or failed)."""
        block = envelope.get(name) if name == _GUARDRAILS else components.get(name)
        if not isinstance(block, dict):
            return {}
        if name != _GUARDRAILS and block.get("status") != STATUS_OK:
            return {}
        return block

    present = [
        (name, key)
        for name, key in (_INDEX_LANES if index else _LANES)
        if isinstance(container(name).get(key), list)
    ]
    emptied = {name: {**container(name), key: [], "truncated": True} for name, key in present}
    shell = {
        **envelope,
        **{name: block for name, block in emptied.items() if name == _GUARDRAILS},
        "components": {
            **components,
            **{name: block for name, block in emptied.items() if name != _GUARDRAILS},
        },
    }
    if "changes" in dict(present):
        # A cut changes page gets a keyset next_cursor afterwards: reserve its room.
        shell["components"]["changes"]["next_cursor"] = "x" * CHANGE_CURSOR_RESERVE
    droppable = None if index else "context_summary"
    budget = max_chars - json_chars(shell)
    if droppable is not None:
        budget -= len('"context_summary_omitted":true,')
    bounded, cut, dropped = fit_lanes(
        [container(name)[key] for name, key in present], budget, droppable=droppable
    )
    out = dict(envelope)
    new_components = dict(components)
    for (name, key), items, was_cut in zip(present, bounded, cut, strict=True):
        block = {**container(name), key: items}
        if was_cut:
            block["truncated"] = True
            if name == "changes":
                block["has_more"] = True
                kept = len(items)
                block["next_cursor"] = (
                    change_cursors[kept - 1] if kept else window_start_cursor(since)
                )
        if name == _GUARDRAILS:
            out[name] = block
        else:
            new_components[name] = block
    out["components"] = new_components
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
