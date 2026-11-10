"""The optional recall ``hint`` towards ``list`` (#2754).

Answering agents rarely call ``list`` for "every item of a kind" questions and
answer from the top rows of a recall instead. When the candidate pool the
recall already holds has more memories with a tag than the page shows, one line
says so and names the ``list`` call that returns every memory with the tag.

The count is over the recall's own candidate pool (the hits that passed the same
filters and shadowing as the page, before the top-k cut), so it is "at least N
matching this search", never a context-wide count: that would be another query.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence

HINT_TEMPLATE = (
    "At least {matched} memories matching this search carry tag {tag}, {shown} shown; "
    'list(filters={{"tags": [{tag}]}}) returns every memory with it.'
)


def _count(rows: Iterable[Sequence[str] | None]) -> Counter[str]:
    """Rows carrying each tag (a tag listed twice on one row counts once)."""
    counts: Counter[str] = Counter()
    for tags in rows:
        counts.update(set(tags or ()))
    return counts


def list_hint(
    page_tags: Sequence[Sequence[str] | None],
    pool_tags: Sequence[Sequence[str] | None],
) -> str | None:
    """Choose the tag to point at ``list`` for, or None.

    Args:
        page_tags: The tags of each row on the page that ships.
        pool_tags: The tags of each candidate in the pool the page was cut from
            (the page's own rows included).

    Returns:
        One line naming the tag with the most pool candidates among those carried
        by a page row whose pool count is above its page count (ties: more page
        rows, then alphabetical), or None when no tag qualifies. The tag is
        JSON-escaped so the line can be pasted as a ``list`` argument.
    """
    shown = _count(page_tags)
    if not shown:
        return None
    matched = _count(pool_tags)
    qualifying = [tag for tag, count in shown.items() if matched[tag] > count]
    if not qualifying:
        return None
    tag = min(qualifying, key=lambda t: (-matched[t], -shown[t], t))
    return HINT_TEMPLATE.format(
        matched=matched[tag], shown=shown[tag], tag=json.dumps(tag, ensure_ascii=False)
    )
