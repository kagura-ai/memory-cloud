"""Read-time tag resolution against a context's vocabulary (Issue #1503).

``recall`` tag filters are exact-match against the Qdrant payload, so a
writer-side spelling drift makes matching memories invisible with no signal that
near-miss tags exist — the caller cannot tell "nothing is stored" apart from
"spelled differently".

Two opt-in read-time affordances, both driven by the tags actually present in
the context:

* :func:`expand_tag_filter` — widen the filter to every stored spelling that is
  a MECHANICAL variant of what was asked for (case / separators / plural).
  Applied only when the caller passes ``filters.tags_normalize = true``, so
  exact semantics remain the default.
* :func:`suggest_tags` — advisory hints when a tag filter matched nothing.
  Includes looser relations (abbreviation, typo) that must never widen a filter.

Both read the vocabulary with one bounded query. The caller is responsible for
having authorized the (workspace, context) first — these helpers do no access
check of their own and must never be reachable from an unauthorized path.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.logger import get_logger
from utils.tag_normalize import is_near_duplicate, normalize_tag

logger = get_logger(__name__)

# Bound on the vocabulary read. A context past this many distinct tags has a
# hygiene problem of its own (#746); truncating keeps one recall from scanning
# an unbounded set, at the cost of missing rare tags in the tail.
VOCABULARY_LIMIT = 2000

# Bound on how many spellings one requested tag may expand to, so a pathological
# vocabulary cannot inflate the Qdrant filter without limit.
MAX_EXPANSION_PER_TAG = 20

# Bound on suggestions returned per requested tag.
MAX_SUGGESTIONS_PER_TAG = 5


async def fetch_vocabulary(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
) -> dict[str, int]:
    """Distinct tag -> memory count for one context, as THIS caller may read it.

    ``DISTINCT id, unnest(tags)`` semantics are not needed here: the outer
    aggregate counts distinct memory ids per tag directly, so a memory carrying
    the same tag twice still counts once (the #614 lesson).

    The ``user_id`` filter mirrors the one ``SearchService`` applies to recall:
    in a context that is not shared, a caller sees only their OWN memories, so
    aggregating over every author would let tag names and counts describe rows
    the caller cannot read. Sharing is resolved once per call, and only a shared
    context aggregates across authors.

    Args:
        db: Session.
        workspace_id: Authorized workspace.
        context_id: Authorized context.
        user_id: Caller identity, used to scope the aggregate.

    Returns:
        ``{tag: memory_count}``, capped at ``VOCABULARY_LIMIT`` by descending count.
    """
    from services.context_service import ContextService

    shared = await ContextService(db).is_context_shared(context_id)

    conditions = [
        Memory.workspace_id == workspace_id,
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
    ]
    if not shared:
        conditions.append(Memory.user_id == user_id)

    tag = func.unnest(Memory.tags).label("tag")
    inner = select(Memory.id.label("memory_id"), tag).where(*conditions).subquery()
    stmt = (
        select(inner.c.tag, func.count(func.distinct(inner.c.memory_id)))
        .group_by(inner.c.tag)
        .order_by(func.count(func.distinct(inner.c.memory_id)).desc())
        .limit(VOCABULARY_LIMIT)
    )
    rows = await db.execute(stmt)
    return {row[0]: row[1] for row in rows.all() if row[0]}


async def expand_tag_filter(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
    tags: list[str],
) -> tuple[list[str], dict[str, list[str]]]:
    """Widen tags to every stored MECHANICAL variant of each requested tag.

    Args:
        db: Session (caller must already have authorized the context).
        workspace_id: Authorized workspace.
        context_id: Authorized context.
        user_id: Caller identity (scopes the vocabulary read).
        tags: Tags the caller filtered on.

    Returns:
        ``(expanded, added_by_tag)``. ``expanded`` preserves the caller's tags
        (even ones absent from the vocabulary, so the filter never silently
        becomes broader-but-different) plus the variants found.
        ``added_by_tag`` maps each requested tag to the extra spellings it
        gained, for telemetry and the response hint; empty when nothing drifted.

    Note:
        Returns the input unchanged if the vocabulary read fails — widening is
        an enhancement and must never break a recall.
    """
    try:
        vocabulary = await fetch_vocabulary(
            db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
        )
    except Exception as e:  # noqa: BLE001 — enhancement must not break recall
        logger.warning("tag_vocabulary_read_failed", error=str(e))
        return tags, {}

    by_normalized: dict[str, list[str]] = {}
    for stored in vocabulary:
        folded = normalize_tag(stored)
        if folded:
            by_normalized.setdefault(folded, []).append(stored)

    expanded: list[str] = []
    seen: set[str] = set()
    added_by_tag: dict[str, list[str]] = {}

    for requested in tags:
        if requested not in seen:
            expanded.append(requested)
            seen.add(requested)
        folded = normalize_tag(requested)
        if not folded:
            continue
        variants = [v for v in by_normalized.get(folded, []) if v != requested][
            :MAX_EXPANSION_PER_TAG
        ]
        gained = []
        for variant in variants:
            if variant not in seen:
                expanded.append(variant)
                seen.add(variant)
                gained.append(variant)
        if gained:
            added_by_tag[requested] = gained

    return expanded, added_by_tag


async def suggest_tags(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
    tags: list[str],
) -> dict[str, list[str]]:
    """Near-duplicate tags that exist in the context, for a filter that matched nothing.

    Args:
        db: Session (caller must already have authorized the context).
        workspace_id: Authorized workspace.
        context_id: Authorized context.
        user_id: Caller identity (scopes the vocabulary read).
        tags: Tags the caller filtered on.

    Returns:
        ``{requested_tag: ["stored-tag (count)", ...]}``, omitting requested
        tags with no near-duplicate. Empty dict when nothing is close — which is
        itself the signal that the topic genuinely is not stored, rather than
        misspelled.
    """
    try:
        vocabulary = await fetch_vocabulary(
            db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
        )
    except Exception as e:  # noqa: BLE001 — a hint must not break recall
        logger.warning("tag_vocabulary_read_failed", error=str(e))
        return {}

    suggestions: dict[str, list[str]] = {}
    for requested in tags:
        hits = [
            f"{stored} ({count})"
            for stored, count in vocabulary.items()
            if stored != requested and is_near_duplicate(requested, stored)
        ][:MAX_SUGGESTIONS_PER_TAG]
        if hits:
            suggestions[requested] = hits
    return suggestions
