"""Write-time recall-ability lint (Issue #1502).

Recall quality depends on how carefully the WRITER crafted ``summary`` and
``tags``, and there is no feedback loop: a sloppy write degrades future recall
silently, discovered only when a later session gets low-confidence results. The
``remember`` docstring teaches good style with explicit good/bad examples, but
nothing checks compliance at write time.

This produces advisory hints on the write response. Design constraints that
shape every rule below:

* **Never blocking.** The memory is already committed when these run. A lint
  failure must never affect the write, so the whole pass is wrapped by the
  caller and any error yields no hints rather than an error response.
* **Only mechanical, checkable claims.** No rule may guess at meaning. Each one
  below is either a threshold the codebase already enforces elsewhere, or a
  vocabulary comparison against tags that demonstrably exist.
* **Silence is the common case.** A well-formed write returns no hints, so the
  field's presence is itself the signal. Rules that fire on ordinary good
  writes are worse than no rule.

The thresholds are imported from the request schema rather than restated, so
the advice a writer receives cannot drift from the guidance the schema already
logs server-side.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.schemas import WriteLintHint
from utils.logger import get_logger
from utils.tag_normalize import is_near_duplicate, normalize_tag

logger = get_logger(__name__)

# Bound on hints returned, so a pathological write cannot inflate the response.
MAX_HINTS = 6

# Bound on near-duplicate tag comparisons — the vocabulary read is already
# capped, this caps the O(tags x vocabulary) walk on top of it.
MAX_TAGS_CHECKED = 20

# Leading phrases that mark a summary as a record of an EVENT rather than the
# reusable conclusion — the "❌ Discussed auth errors in today's meeting" case
# from the remember() guidance. Deliberately anchored to the start of the
# summary and deliberately short: a conclusion-first summary can legitimately
# mention a meeting later in the sentence, and only the opening establishes
# what the summary is *about*.
_NARRATIVE_OPENERS = re.compile(
    r"^\s*(?:"
    r"discussed|talked about|met (?:with|about)|meeting (?:about|on|notes)|"
    r"today (?:i|we)\b|we (?:discussed|talked|decided to talk)|"
    r"notes? (?:from|on)\b|summary of (?:the )?(?:meeting|call|discussion)|"
    r"話し合った|打ち合わせ|会議(?:メモ|録)|本日は|今日は"
    r")",
    re.IGNORECASE,
)


def _summary_hints(summary: str) -> list[WriteLintHint]:
    """Style checks on the searchable layer."""
    from models.schemas import SUMMARY_LONG_THRESHOLD, SUMMARY_SHORT_THRESHOLD

    hints: list[WriteLintHint] = []
    length = len(summary)

    if length < SUMMARY_SHORT_THRESHOLD:
        hints.append(
            WriteLintHint(
                code="summary_short",
                hint=(
                    f"summary is {length} chars; under {SUMMARY_SHORT_THRESHOLD} rarely "
                    "carries enough terms to match a future query. Add the domain "
                    "and the outcome."
                ),
            )
        )
    elif length > SUMMARY_LONG_THRESHOLD:
        hints.append(
            WriteLintHint(
                code="summary_long",
                hint=(
                    f"summary is {length} chars; over {SUMMARY_LONG_THRESHOLD} dilutes "
                    "the embedding. Consider splitting into several semantic memories."
                ),
            )
        )

    if _NARRATIVE_OPENERS.match(summary):
        hints.append(
            WriteLintHint(
                code="summary_narrative",
                hint=(
                    "summary opens as a record of an event; state the reusable "
                    "conclusion instead (what was decided or found, not that it "
                    "was discussed)."
                ),
            )
        )

    return hints


def _tag_hints(tags: list[str], vocabulary: dict[str, int]) -> list[WriteLintHint]:
    """Vocabulary checks — only ever comparing against tags that exist."""
    hints: list[WriteLintHint] = []

    if not tags:
        hints.append(
            WriteLintHint(
                code="no_tags",
                hint=(
                    "no tags: this memory cannot be reached by a tag filter. "
                    "Call list_tags() to reuse the spellings already in this context."
                ),
            )
        )
        return hints

    for tag in tags[:MAX_TAGS_CHECKED]:
        folded = normalize_tag(tag)
        if not folded or tag in vocabulary:
            continue  # already an established spelling — nothing to say
        matches = [
            (stored, count)
            for stored, count in vocabulary.items()
            if is_near_duplicate(tag, stored)
        ]
        if not matches:
            continue
        stored, count = max(matches, key=lambda pair: pair[1])
        hints.append(
            WriteLintHint(
                code="tag_near_duplicate",
                hint=(
                    f"tag '{tag}' is new but resembles '{stored}' ({count} memories). "
                    "Reusing the established spelling keeps tag filters working."
                ),
                subject=tag,
            )
        )

    return hints


async def lint_write(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
    summary: str,
    tags: list[str] | None,
) -> list[WriteLintHint]:
    """Advisory recall-ability hints for a just-written memory.

    Args:
        db: Session (caller must already have authorized the context).
        workspace_id: Authorized workspace.
        context_id: Authorized context.
        user_id: Caller identity — scopes the vocabulary read so a hint can
            never describe memories the caller cannot read.
        summary: The summary as written.
        tags: The tags as written.

    Returns:
        Up to ``MAX_HINTS`` hints, or an empty list when the write looks fine —
        and on ANY internal error, since the memory is already committed and a
        lint must never turn a successful write into a failure.
    """
    try:
        hints = _summary_hints(summary)

        tag_list = [t for t in (tags or []) if isinstance(t, str) and t]
        vocabulary: dict[str, int] = {}
        if tag_list:
            # Only pay for the vocabulary read when there is something to compare
            # against it; the no-tags hint needs no vocabulary at all.
            from services.tag_resolution import fetch_vocabulary

            vocabulary = await fetch_vocabulary(
                db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
            )
        hints.extend(_tag_hints(tag_list, vocabulary))

        return hints[:MAX_HINTS]
    except Exception as e:  # noqa: BLE001 — advisory only; never fail a committed write
        logger.warning("write_lint_failed", error=str(e))
        return []
