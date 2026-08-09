"""Tag folding for drift-tolerant reads (Issue #1503).

Tag filters are exact-match, so a writer-side spelling drift silently empties or
thins a reader's results with no signal that near-miss tags exist. This module
defines the two relations the read path uses:

* :func:`normalize_tag` — the MECHANICAL fold. ``Dev_Environment``,
  ``dev-environment`` and ``dev environment`` are the same tag written three
  ways; folding them is safe because no author means them differently.
* :func:`is_near_duplicate` — the ADVISORY relation, deliberately looser and
  never used to widen a filter. It powers ``tag_suggestions``: a hint that a
  similar tag exists, which the caller decides what to do with.

The split matters. The issue's motivating example is ``dev-env`` vs
``dev-environment`` — an ABBREVIATION, which no case/separator/plural fold and
no edit-distance-2 threshold will ever unify (they differ by 7 edits). Silently
matching it would mean guessing at authorial intent. So abbreviations surface as
a suggestion, and only mechanical variants actually widen the filter.
"""

from __future__ import annotations

import re
import unicodedata

# Separator characters an author might use interchangeably inside one tag.
_SEPARATORS = re.compile(r"[\s_\-./]+")

# Minimum length before the prefix/containment heuristic is allowed to fire, so
# short tags ("ci", "db", "go") do not suggest every tag that starts with them.
_MIN_AFFIX_LEN = 4

# Bound on edit distance for the typo heuristic, and the length below which even
# one edit is too loose to be a useful suggestion.
_MAX_EDIT_DISTANCE = 2
_MIN_EDIT_LEN = 5


def normalize_tag(tag: str) -> str:
    """Fold a tag to its mechanical-variant-insensitive form.

    Applies, in order: NFKC (so full-width and half-width Latin fold together,
    matching how ``utils.text.normalize_for_search`` treats searchable text),
    case folding, separator removal, and a conservative plural strip.

    Args:
        tag: Raw tag string.

    Returns:
        The folded form. Two tags with the same folded form are the same tag
        written differently; an empty string means the tag carried no folding
        signal (e.g. it was only punctuation) and must not be matched on.

    Example:
        >>> normalize_tag("Dev-Environment") == normalize_tag("dev_environment")
        True
        >>> normalize_tag("troubleshooting") == normalize_tag("Troubleshooting")
        True
        >>> normalize_tag("dev-env") == normalize_tag("dev-environment")
        False
    """
    folded = unicodedata.normalize("NFKC", tag).strip().casefold()
    folded = _SEPARATORS.sub("", folded)
    return _strip_plural(folded)


def _strip_plural(folded: str) -> str:
    """Strip a trailing English plural, conservatively.

    Only ``-ies -> -y`` and a bare trailing ``-s`` are folded, and never on a
    stem short enough that the result would collide with unrelated tags. Words
    ending in ``-ss`` (``class``, ``progress``) are left alone. Non-Latin tags
    are unaffected because they do not end in ``s``.
    """
    if len(folded) > 4 and folded.endswith("ies"):
        return folded[:-3] + "y"
    if len(folded) > 3 and folded.endswith("s") and not folded.endswith("ss"):
        return folded[:-1]
    return folded


def _edit_distance_within(a: str, b: str, limit: int) -> bool:
    """True when Levenshtein(a, b) <= limit. Bounded, so it exits early."""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def is_near_duplicate(requested: str, candidate: str) -> bool:
    """Whether ``candidate`` is worth SUGGESTING for a ``requested`` tag.

    Advisory only — never used to widen a filter, because each rule below can
    relate tags an author meant to keep distinct.

    Fires when the folded forms are:

    * equal — a pure mechanical variant (also matched by the filter widening);
    * one an affix of the other, both at least 4 chars — catches the
      abbreviation case (``dev-env`` / ``dev-environment``) that no distance
      threshold reaches;
    * within 2 edits, both at least 5 chars — catches typos.

    Args:
        requested: The tag the caller filtered on.
        candidate: A tag that exists in the context's vocabulary.

    Returns:
        True if the pair should be surfaced as a suggestion.
    """
    a, b = normalize_tag(requested), normalize_tag(candidate)
    if not a or not b or a == b:
        return bool(a) and a == b
    if len(a) >= _MIN_AFFIX_LEN and len(b) >= _MIN_AFFIX_LEN:
        if a.startswith(b) or b.startswith(a) or a.endswith(b) or b.endswith(a):
            return True
    if len(a) >= _MIN_EDIT_LEN and len(b) >= _MIN_EDIT_LEN:
        return _edit_distance_within(a, b, _MAX_EDIT_DISTANCE)
    return False
