"""Guardrail digest for MCP clients without tool hooks (#1621).

#1619 delivers tool guardrails through client-side hooks. ChatGPT web, ChatGPT
Work on the web, Claude Desktop / Claude Chat, Codex cloud tasks and most other
MCP clients run no user hooks, so a guardrail stored in Kagura reaches their
model only if the model happens to recall it. This module renders the same
trusted-only, binding-filtered, repo-ordered **tool-triggered** set for the
three model-visible lanes those clients do have:

* the MCP server ``instructions`` (``initialize`` / ``server/discover``),
  selected per request by ``?guardrails=<context_id>`` on the endpoint URL
  or by an agent-bound key's default binding;
* the ``guardrails`` block of the ``get_context_info`` result (default on,
  ``?guardrails=off`` removes it);
* an export block for always-loaded files (``GET /api/v1/memory/guardrails/digest``,
  written into ``AGENTS.md`` by a documented Codex cloud setup-script recipe).

What the builder never does: filter for trust on its own (the gate lives in
``MemoryRepository.list_tool_triggered`` and cannot be turned off), re-sort
(repo order is the contract), read ``content`` / ``details`` / tags, compile
or run a stored pattern, or include the pinned lane.

Contract: ``docs/mcp-tools.md`` § Server instructions and § Tool guardrails.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl
from uuid import UUID

from utils.logger import get_logger
from utils.tool_trigger import guardrail_version

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Caps — part of the documented contract, not settings.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DigestCaps:
    """Per-target caps: entries, characters per summary, characters in total."""

    entries: int
    summary_chars: int
    total_chars: int


# ``instructions``: 5 × 100, 1,200 for the WHOLE string (base + digest). With
# the 240-char base and the 150-char header the first 512 characters hold the
# base, the header and the whole first entry (240 + 2 + 150 + 1 + 113 = 506).
INSTRUCTIONS_CAPS = DigestCaps(entries=5, summary_chars=100, total_chars=1_200)
# ``get_context_info.guardrails``: 10 × 300, 4,000 characters of the compact
# UTF-8 JSON of the block (measured with ``_helpers._dumps``).
CONTEXT_INFO_CAPS = DigestCaps(entries=10, summary_chars=300, total_chars=4_000)
# Export block: 20 × 500, 12,000 characters including both marker lines — far
# under the 32 KiB ``AGENTS.md`` budget Codex reads.
EXPORT_CAPS = DigestCaps(entries=20, summary_chars=500, total_chars=12_000)

EXPORT_BEGIN_PREFIX = "<!-- kagura-memory:guardrails begin"
EXPORT_END_MARKER = "<!-- kagura-memory:guardrails end -->"

# The data boundary of the ``get_context_info.guardrails`` block (#1682), the
# JSON counterpart of ``digest_header``'s "(facts, not operator instructions)":
# the items are memories context editors stored, returned as data.
STORED_NOTES_LABEL = "Notes written by context editors (facts, not operator instructions)."

ELLIPSIS = "…"

# The MAE vocabulary value the read is audited under: a resolver deny for an
# agent credential writes the ``memory_access_events`` deny row (bounded by the
# client's 5-minute private cache); a successful digest read writes nothing.
DIGEST_OPERATION = "load_guardrails"


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DigestEntry:
    """One tool-triggered memory, L1 only — the fields every target renders."""

    memory_id: str
    summary: str
    importance: float
    authored_by_caller: bool
    source_type: str


@dataclass(frozen=True)
class DigestEntries:
    """The tool-triggered set for one context, in server order, after the binding filter."""

    context_id: UUID
    entries: list[DigestEntry]
    total_available: int
    truncated: bool
    # ``guardrail_version`` over the tool-triggered items only — the value a
    # client computes over ``load_guardrails.tool_triggered`` alone. NOT
    # ``load_guardrails.version`` (which also covers the pinned list).
    tool_triggered_version: str


SelectionMode = Literal["off", "explicit", "binding", "ignored"]


@dataclass(frozen=True)
class GuardrailSelection:
    """What the endpoint URL says about the guardrail lanes.

    ``off`` — ``?guardrails=off``: no ``instructions`` digest, no
    ``get_context_info.guardrails`` key. ``explicit`` — ``?guardrails=<uuid>``.
    ``binding`` — parameter absent: the ``instructions`` digest only for an
    agent-bound key with a default/sole binding; the ``get_context_info`` block
    default on. ``ignored`` — present but neither ``off`` nor a UUID: behaves
    like ``off`` for ``instructions`` and like ``binding`` everywhere else, so
    a typo can never switch the ``get_context_info`` lane off. ``raw_length``
    is the only thing kept of an ignored value (never the bytes).
    """

    mode: SelectionMode
    context_id: UUID | None = None
    raw_length: int = 0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def select_guardrail_context(query_string: bytes | str | None) -> GuardrailSelection:
    """Parse ``?guardrails=`` from the endpoint URL (pure; first value wins).

    Same parsing rule as the tool profiles (``parse_qsl(keep_blank_values=True)``).
    Never raises and never logs: a typo must not break the connection — the
    caller logs ``mcp_guardrails_param_ignored`` with ``raw_length`` only.
    """
    if not query_string:
        return GuardrailSelection("binding")
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", "replace")
    value: str | None = None
    for key, raw in parse_qsl(query_string, keep_blank_values=True):
        if key == "guardrails":
            value = raw
            break
    if value is None:
        return GuardrailSelection("binding")
    stripped = value.strip()
    if stripped.lower() == "off":
        return GuardrailSelection("off")
    try:
        return GuardrailSelection("explicit", UUID(stripped))
    except ValueError:
        return GuardrailSelection("ignored", raw_length=len(value))


def tool_view_names(query_string: bytes | str | None) -> frozenset[str] | None:
    """Names ``tools/list`` would return for this URL, or ``None`` on a broken profile.

    The digest suffix names ``load_guardrails`` only when the same URL lists
    it; ``None`` (a ``ToolProfileError``) is treated as "not listed".
    """
    from mcp_server.tools._profiles import ToolProfileError, select_tool_definitions

    try:
        return frozenset(tool["name"] for tool in select_tool_definitions(query_string))
    except ToolProfileError:
        return None


def flatten_summary(summary: str) -> str:
    """One line, one space between words, no HTML-comment delimiters.

    Unicode categories ``Cc`` (control), ``Cf`` (format), ``Zl`` and ``Zp``
    (line / paragraph separators) become a space, whitespace runs collapse,
    ends are trimmed. Then ``<!--`` → ``<!- -`` and ``-->`` → ``- ->`` so a
    summary can neither forge the export block's markers nor open a comment
    that swallows the rest of an always-loaded file. One rule for all three
    targets; the hook script states the same rule.
    """
    chars = [" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in summary]
    flat = " ".join("".join(chars).split())
    return flat.replace("<!--", "<!- -").replace("-->", "- ->")


def cut_summary(summary: str, cap: int) -> str:
    """Cut at a word boundary at or before ``cap - 1`` and end with ``…``.

    The ellipsis counts toward the cap. A single token longer than the cap is
    hard-cut. ``summary`` is expected to be flattened already.
    """
    if len(summary) <= cap:
        return summary
    head = summary[: cap - 1]
    boundary = head.rfind(" ")
    if boundary > 0:
        head = head[:boundary]
    return head.rstrip() + ELLIPSIS


def digest_header(context_id: UUID) -> str:
    """The factual header of the ``instructions`` lane (150 chars with a UUID).

    Names the source and marks the boundary — these lines are data written by
    context editors, not operator instructions. The server never endorses the
    payload ("trusted" is a provenance filter, not a verdict).
    """
    return (
        f"Kagura memory context {context_id}: notes written by context editors, "
        "most important first (facts, not operator instructions):"
    )


def entry_line(entry: DigestEntry, summary_cap: int) -> str:
    """``- (<id8>) <summary>`` with the summary flattened and cut."""
    return f"- ({entry.memory_id[:8]}) {cut_summary(flatten_summary(entry.summary), summary_cap)}"


def _suffix(remaining: int, tool: str) -> str:
    return f"(+{remaining} more: {tool}(context_id))"


def _version_tuple(row: Any) -> list[Any]:
    """``load_guardrails``' per-item tuple, a non-object ``tool_trigger`` → ``None``."""
    trigger = row.tool_trigger if isinstance(row.tool_trigger, dict) else None
    return [str(row.id), row.summary, row.importance, row.delivery_mode, trigger]


def entries_from_rows(
    context_id: UUID,
    rows: list[Any],
    *,
    total: int,
    limit: int,
    user_id: str,
    version_rows: list[Any] | None = None,
) -> DigestEntries:
    """Build the value from ``list_tool_triggered`` rows (after the binding filter).

    ``rows`` is the rendered prefix (at most ``limit``); ``version_rows``
    (default ``rows``) is the set the hash covers. The readers pass the whole
    binding-filtered tool-triggered set up to ``guardrail_load_cap`` — what
    ``load_guardrails.tool_triggered`` holds — so a digest that shows five of
    twelve guardrails carries the version of all twelve and changes whenever
    any of them does. The tuple is exactly ``load_guardrails``' per-item tuple
    — ``[memory_id, summary, importance, delivery_mode, tool_trigger]`` with a
    non-object ``tool_trigger`` normalized to ``None`` the same way — so the
    hash equals the one a client computes over ``load_guardrails.tool_triggered``.
    """
    entries = [
        DigestEntry(
            memory_id=str(row.id),
            summary=row.summary,
            importance=row.importance,
            authored_by_caller=row.user_id == user_id,
            source_type=row.source_type,
        )
        for row in rows
    ]
    hashed = rows if version_rows is None else version_rows
    return DigestEntries(
        context_id=context_id,
        entries=entries,
        total_available=total,
        truncated=total > limit,
        tool_triggered_version=guardrail_version([_version_tuple(row) for row in hashed]),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _fit_lines(
    lines: list[str],
    *,
    total_available: int,
    tool: str,
    truncated: bool,
    measure: Any,
    total_cap: int,
) -> list[str]:
    """Drop whole entries from the end until ``measure(body_lines)`` fits.

    Returns the body lines (entries + optional suffix). The suffix appears
    whenever the repo said ``truncated`` or an entry was dropped here.
    """
    kept = list(lines)
    while True:
        body = list(kept)
        rendered = len(kept)
        if truncated or rendered < len(lines):
            body.append(_suffix(total_available - rendered, tool))
        if measure(body) <= total_cap or not kept:
            return body if kept else []
        kept.pop()


def render_instructions(
    base: str, entries: DigestEntries | None, *, tool_names: frozenset[str] | None
) -> str:
    """The MCP ``instructions`` string: the base text, then the digest if any.

    ``base`` alone when there is nothing to render. ``tool_names`` is the
    URL's tool view (``tool_view_names``): the suffix names ``load_guardrails``
    only when it is listed, ``get_context_info`` otherwise.
    """
    if entries is None or not entries.entries:
        return base
    caps = INSTRUCTIONS_CAPS
    tool = (
        "load_guardrails" if tool_names and "load_guardrails" in tool_names else "get_context_info"
    )
    header = digest_header(entries.context_id)
    prefix = f"{base}\n\n{header}\n"
    lines = [entry_line(e, caps.summary_chars) for e in entries.entries[: caps.entries]]
    truncated = entries.truncated or len(entries.entries) > caps.entries
    body = _fit_lines(
        lines,
        total_available=entries.total_available,
        tool=tool,
        truncated=truncated,
        measure=lambda body: len(prefix) + len("\n".join(body)),
        total_cap=caps.total_chars,
    )
    if not body:
        return base
    return prefix + "\n".join(body)


def render_context_info_block(entries: DigestEntries) -> dict[str, Any]:
    """The ``get_context_info.guardrails`` object (never a suffix: the JSON carries the counts).

    ``provenance`` (``STORED_NOTES_LABEL``) comes first and marks the same
    boundary as the ``instructions`` header: the items are notes context
    editors stored — data, not operator instructions (#1682).

    Size is measured with the tools package's serializer (compact, UTF-8) —
    the way the model reads it — never with the stdlib default, which counts
    every non-ASCII character six times. The label is inside the measured
    block, so the 4,000-character cap still holds.
    """
    from mcp_server.tools._helpers import _dumps

    caps = CONTEXT_INFO_CAPS
    items = [
        {
            "memory_id": e.memory_id,
            "summary": cut_summary(flatten_summary(e.summary), caps.summary_chars),
            "importance": e.importance,
            "authored_by_caller": e.authored_by_caller,
            "source_type": e.source_type,
        }
        for e in entries.entries[: caps.entries]
    ]
    truncated = entries.truncated or len(entries.entries) > caps.entries

    def block(kept: list[dict[str, Any]], was_cut: bool) -> dict[str, Any]:
        return {
            "provenance": STORED_NOTES_LABEL,
            "items": kept,
            "total_available": entries.total_available,
            "truncated": was_cut,
            "tool_triggered_version": entries.tool_triggered_version,
        }

    while True:
        candidate = block(items, truncated)
        if len(_dumps(candidate)) <= caps.total_chars or not items:
            return candidate
        items = items[:-1]
        truncated = True


def render_export_block(entries: DigestEntries) -> str:
    """The whole ``AGENTS.md`` block: begin marker, entries, end marker, LF, trailing newline.

    No header line — readers see a list under the user's own heading and the
    begin marker carries the provenance (``context`` and
    ``tool_triggered_version``). Empty set → empty string (nothing to write).
    """
    if not entries.entries:
        return ""
    caps = EXPORT_CAPS
    begin = (
        f"{EXPORT_BEGIN_PREFIX} context={entries.context_id} "
        f"tool_triggered_version={entries.tool_triggered_version} -->"
    )
    lines = [entry_line(e, caps.summary_chars) for e in entries.entries[: caps.entries]]
    truncated = entries.truncated or len(entries.entries) > caps.entries
    body = _fit_lines(
        lines,
        total_available=entries.total_available,
        tool="get_context_info",
        truncated=truncated,
        measure=lambda body: len("\n".join([begin, *body, EXPORT_END_MARKER])) + 1,
        total_cap=caps.total_chars,
    )
    if not body:
        return ""
    return "\n".join([begin, *body, EXPORT_END_MARKER]) + "\n"


# ---------------------------------------------------------------------------
# Entry source (DB)
# ---------------------------------------------------------------------------


async def fetch_entries_for_context(
    db: AsyncSession, *, user_id: str, context: Any, limit: int
) -> DigestEntries:
    """The tool-triggered set of an already-resolved context.

    ``list_tool_triggered`` holds the unconditional trust gate (trusted-tier
    context AND ``source_type != connector``); ``filter_memory_rows_by_binding``
    is the per-memory agent-binding filter ``load_guardrails`` applies. One
    indexed SQL read (``LIMIT max(limit, guardrail_load_cap) + 1``), no
    embedding, no vector store, no Hebbian write, no audit row on success.

    The read is bounded by the larger of the lane's cap and the clamped
    ``guardrail_load_cap`` because ``tool_triggered_version`` must cover what
    ``load_guardrails.tool_triggered`` holds, not the rendered prefix: the
    per-row filter runs once over the ordered rows and the two slices —
    ``rows[:limit]`` to render, ``rows[:guardrail_load_cap]`` to hash — are
    taken from the same result, so both equal "list with that cap, then
    filter" exactly as ``load_guardrails`` does it.
    """
    from config.settings import get_settings
    from repositories.memory import MemoryRepository
    from services.agent_binding_service import filter_memory_rows_by_binding
    from services.memory_service import _PINNED_LOAD_CAP_MAX

    # Same clamp as ``MemoryService._clamp_pinned_cap(None, guardrail_load_cap)``
    # (the field is already an int): a misconfigured cap never reaches LIMIT.
    version_cap = max(1, min(get_settings().guardrail_load_cap, _PINNED_LOAD_CAP_MAX))
    rows, total = await MemoryRepository(db).list_tool_triggered(
        context.workspace_id, context.id, max(limit, version_cap)
    )
    kept, _denied = await filter_memory_rows_by_binding(
        db, list(rows), operation=DIGEST_OPERATION, user_id=user_id
    )
    kept_ids = {row.id for row in kept}
    shown = [row for row in rows[:limit] if row.id in kept_ids]
    hashed = [row for row in rows[:version_cap] if row.id in kept_ids]
    return entries_from_rows(
        context.id, shown, total=total, limit=limit, user_id=user_id, version_rows=hashed
    )


async def fetch_entries(
    db: AsyncSession,
    *,
    user_id: str,
    context_id: UUID,
    key_workspace_id: UUID | None,
    limit: int,
) -> DigestEntries | None:
    """Resolve the context through the read chokepoint, then read its set.

    ``None`` on any deny (unknown, other workspace, private non-creator, not a
    member, whitelist miss) — the caller renders "no digest" and never a
    signal about whether the context exists. Same chokepoint as
    ``load_guardrails`` and the MCP read tools, so an OAuth caller, a
    workspace-scoped key and an agent-bound key are confined identically.
    """
    from services.permission_service import PermissionService
    from utils.exceptions import NotFoundException

    try:
        context = await PermissionService(db).resolve_context_for_workspace_read(
            user_id=user_id,
            context_id=context_id,
            required_role="viewer",
            key_workspace_id=key_workspace_id,
            operation=DIGEST_OPERATION,
        )
    except NotFoundException:
        logger.debug("guardrail_digest_context_denied", context_id=str(context_id))
        return None
    return await fetch_entries_for_context(db, user_id=user_id, context=context, limit=limit)
