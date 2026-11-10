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
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.schemas import WriteLintHint
from utils.event_dates import resolved_event_days
from utils.logger import get_logger
from utils.tag_normalize import is_near_duplicate, is_specialisation, normalize_tag

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


# #2752: a parenthesised group right after the event phrase that holds nothing
# but one ISO date or a range of two ("last week (2024-03-04..2024-03-10)").
# The second date may keep only what differs from the first, as the skills
# write it: "(2024-03-09–10)" (DD) or "(2024-02-26–03-03)" (MM-DD).
_ISO_DAY = r"\d{4}-\d{2}-\d{2}"
_BARE_DATES = re.compile(
    rf"\s*\(\s*(?P<start>{_ISO_DAY})"
    r"(?:(?:\s*(?:\.\.|–|—|-|/)\s*|\s+to\s+)"
    rf"(?P<end>{_ISO_DAY}|\d{{2}}-\d{{2}}|\d{{2}}))?\s*\)",
    re.IGNORECASE,
)
# The leading words the resolver ignores ("on Sunday" is written "Sunday" too).
_PHRASE_LEADING = ("on", "in", "the")


def _written_days(match: re.Match[str]) -> tuple[date, date] | None:
    try:
        start = date.fromisoformat(match.group("start"))
        end_text = match.group("end")
        if not end_text:
            end = start
        elif len(end_text) == 2:
            end = start.replace(day=int(end_text))
        elif len(end_text) == 5:
            end = start.replace(month=int(end_text[:2]), day=int(end_text[3:]))
        else:
            end = date.fromisoformat(end_text)
    except ValueError:
        return None  # not a calendar date: not a bare date either
    return start, end


def _event_date_hints(summary: str, content: str | None, details: Any) -> list[WriteLintHint]:
    """``event_date_mismatch``: the text dates the phrase differently from the server.

    Fires only when the memory's ``event_phrase`` resolved (``event_start`` ..
    ``event_end``) and the summary or the content has the phrase
    (case-insensitive, a leading on / in / the optional) followed by a
    parenthesised ISO date or range that is not those days; one date agrees
    with a single-day event, and a range's second date may be shortened to
    ``MM-DD`` or ``DD`` (the missing parts are the first date's). Silent for a
    parenthesis that is not a bare date ("(stated on 2024-03-09)"). Nothing is
    rewritten.
    """
    resolved = resolved_event_days(details)
    if resolved is None:
        return []
    phrase = details["event_phrase"].strip().rstrip(".,!").strip()
    words = phrase.split()
    while len(words) > 1 and words[0].lower() in _PHRASE_LEADING:
        words = words[1:]
    if not words:
        return []
    pattern = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words), re.IGNORECASE)
    start, end = resolved
    shown = start.isoformat() if start == end else f"{start.isoformat()}..{end.isoformat()}"
    for text in (summary, content):
        if not isinstance(text, str):
            continue
        for found in pattern.finditer(text):
            dates = _BARE_DATES.match(text, found.end())
            if dates is None:
                continue
            written = _written_days(dates)
            if written is None or written == resolved:
                continue
            value = dates.group(0).strip()[1:-1].strip()
            return [
                WriteLintHint(
                    code="event_date_mismatch",
                    hint=(
                        f"'{found.group(0)}' is written as ({value}), but the server resolved "
                        f"it to {shown} (event_start..event_end); remove the written date "
                        "or correct it with update_memory()."
                    ),
                    subject=phrase,
                )
            ]
    return []


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


def _tag_hints(
    tags: list[str], vocabulary: dict[str, int], checked: list[str] | None = None
) -> list[WriteLintHint]:
    """Vocabulary checks — only ever comparing against tags that exist.

    ``checked`` (default: ``tags``) are the tags compared; ``tags`` decides
    ``no_tags``.
    """
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

    for tag in (tags if checked is None else checked)[:MAX_TAGS_CHECKED]:
        folded = normalize_tag(tag)
        if not folded or tag in vocabulary:
            continue  # already an established spelling — nothing to say
        # #1617: a compound tag and its own leading segment(s) (session-cookie /
        # session) ARE near-duplicates — the read path wants that — but here the
        # hint would claim a spelling problem that is not one, and once a context
        # holds the short generic tags almost every new compound tag would draw
        # a hint. Dropped before the pick, so a generic tag cannot mask a real
        # variant either.
        matches = [
            (stored, count)
            for stored, count in vocabulary.items()
            if is_near_duplicate(tag, stored) and not is_specialisation(tag, stored)
        ]
        if not matches:
            continue
        # An equal fold is the strongest claim (the same tag written another
        # way); among the rest, point at the dominant spelling. Without the
        # first key, `auth` (50) would outrank `auth_context` (2) for a new
        # `AuthContext`.
        stored, count = max(matches, key=lambda pair: (normalize_tag(pair[0]) == folded, pair[1]))
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
    memory_id: UUID | None = None,
    checked_tags: list[str] | None = None,
    content: str | None = None,
    details: Any = None,
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
        memory_id: The written row, for the log line only.
        checked_tags: The tags to compare with the vocabulary, when not all of
            ``tags`` (#2585: the ones an ``add_tags`` call added; the stored
            ones were judged when they were written). ``tags`` still decides
            ``no_tags``.
        content: The content as stored, for ``event_date_mismatch`` (#2752).
        details: The details as stored (``event_phrase``, ``observed_at``,
            ``event_direction``), for ``event_date_mismatch``.

    Returns:
        Up to ``MAX_HINTS`` hints, or an empty list when the write looks fine —
        and on ANY internal error, since the memory is already committed and a
        lint must never turn a successful write into a failure.
    """
    try:
        hints = _summary_hints(summary)
        hints.extend(_event_date_hints(summary, content, details))

        tag_list = [t for t in (tags or []) if isinstance(t, str) and t]
        checked = (
            tag_list
            if checked_tags is None
            else [t for t in checked_tags if isinstance(t, str) and t]
        )
        vocabulary: dict[str, int] = {}
        if checked:
            # Only pay for the vocabulary read when there is something to compare
            # against it; the no-tags hint needs no vocabulary at all. #1512: the
            # read is the cached snapshot from BEFORE this write (the row is
            # already committed), so the rule fires the same on a hit and a miss.
            from services.tag_resolution import vocabulary_before_write

            vocabulary = await vocabulary_before_write(
                db,
                workspace_id=workspace_id,
                context_id=context_id,
                user_id=user_id,
                written_tags=checked,
            )
        hints.extend(_tag_hints(tag_list, vocabulary, checked))
        hints = hints[:MAX_HINTS]

        if hints:
            # #1512: the counts here are what the caller actually receives, so
            # the fire rate of each rule can be read off the logs.
            logger.info(
                "write_lint_hints",
                context_id=str(context_id),
                memory_id=str(memory_id) if memory_id else None,
                codes=[h.code for h in hints],
                tag_near_duplicate_hints=sum(1 for h in hints if h.code == "tag_near_duplicate"),
            )
        return hints
    except Exception as e:  # noqa: BLE001 — advisory only; never fail a committed write
        logger.warning("write_lint_failed", error=str(e))
        return []
