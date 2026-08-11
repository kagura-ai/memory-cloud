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


# Two-letter endings that are almost never an English plural, so stripping the
# trailing ``s`` would truncate a real word rather than singularise it:
# redis -> redi, status -> statu, chaos -> chao, alias -> alia, class -> clas.
# ``es`` is deliberately ABSENT — it is a genuine plural ending (issues, boxes)
# and excluding it would stop those folding at all.
_NON_PLURAL_ENDINGS = ("ss", "is", "us", "os", "as")

# Tags whose ending is indistinguishable from a plural by any rule (``https``
# looks exactly like ``apps``), but which are common enough that the mangled
# stem collides with a real tag — ``https`` folding to ``http`` would merge two
# tags an author kept distinct. Kept deliberately tiny: an over-strip only does
# harm when the stem is itself a real tag in the same context, so this is not a
# list of every non-plural word ending in s.
_NEVER_PLURAL = frozenset({"https"})


def _strip_plural(folded: str) -> str:
    """Strip a trailing English plural, conservatively.

    Only ``-ies -> -y`` and a bare trailing ``-s`` are folded, never on a stem
    short enough that the result would collide with unrelated tags, and never
    when the word ends in one of ``_NON_PLURAL_ENDINGS``.

    This is deliberately under-inclusive. A missed plural costs one unmatched
    spelling; an over-strip silently merges two tags an author kept distinct,
    and ``expand_tag_filter`` groups the whole vocabulary by this fold — so a
    wrong merge widens a real filter. Non-Latin tags are unaffected because they
    do not end in ``s``.
    """
    if len(folded) > 4 and folded.endswith("ies"):
        return folded[:-3] + "y"
    if (
        len(folded) > 3
        and folded.endswith("s")
        and folded not in _NEVER_PLURAL
        and not folded.endswith(_NON_PLURAL_ENDINGS)
    ):
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
        # Prefix only. A shared SUFFIX is far weaker evidence of abbreviation and
        # relates plainly unrelated tags (test/latest, prod/reprod, auth/oauth),
        # and noisy suggestions are how an agent learns to ignore the field.
        # The case this rule exists for — dev-env / dev-environment — is a prefix.
        if a.startswith(b) or b.startswith(a):
            return True
    if len(a) >= _MIN_EDIT_LEN and len(b) >= _MIN_EDIT_LEN:
        return _edit_distance_within(a, b, _MAX_EDIT_DISTANCE)
    return False
