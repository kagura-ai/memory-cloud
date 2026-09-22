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

A third predicate, :func:`is_specialisation`, is used by the WRITE lint only
(#1617). ``session-cookie`` is a near-duplicate of ``session`` by the prefix
rule, and on the read path that is useful — for a zero-result filter on
``session`` the stored sub-topic is the actionable answer. On the write path
the same relation is a false claim: the hint says the new tag is a misspelling,
and replacing ``session-cookie`` with ``session`` discards information. So the
lint subtracts specialisations from the near-duplicate matches; the read path
does not.
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

# A maximal run of digits, masked to one placeholder to compare two tags as
# members of a numbered series (#1608).
_DIGIT_RUN = re.compile(r"\d+")

# Characters that end one segment of a compound tag and start the next
# (#1617). This is NOT ``_SEPARATORS``: ``:`` and ``#`` are added, because
# ``category:auth`` and ``some-repo#62`` are compounds too, and ``.`` is left
# out, because a dotted name is one identifier written with a dot — ``node`` /
# ``node.js``, ``next`` / ``next.js``, ``socket`` / ``socket.io`` are the same
# thing written two ways and must keep hinting, and ``v0.73`` / ``v0.73.0``
# stays related with no special case. Splitting on ``.`` would silence all
# four.
_SEGMENT_BOUNDARY = re.compile(r"[\s_\-/:#]+")


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


def _digit_skeleton(tag: str) -> str:
    """Fold a tag with every run of digits masked to a single placeholder.

    The mask runs BEFORE :func:`normalize_tag` drops separators, so the number
    of numeric fields survives: ``v0.73.0`` is ``v000`` and ``v0.7`` is ``v00``.
    Masking the folded form instead would merge ``0.73.0`` into one run and call
    those two the same series. NFKC comes first so superscript and circled
    digits, which the regex digit class does not match, are masked like ASCII
    ones (full-width digits match it either way).
    """
    return normalize_tag(_DIGIT_RUN.sub("0", unicodedata.normalize("NFKC", tag)))


def _digit_runs(tag: str) -> list[str]:
    """The tag's numbers with zero padding dropped, so ``07`` and ``7`` compare equal.

    Compared as strings rather than ``int``: a tag is caller-controlled and
    ``int()`` refuses a run longer than the interpreter's digit limit.
    """
    return [run.lstrip("0") for run in _DIGIT_RUN.findall(unicodedata.normalize("NFKC", tag))]


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

    Never fires for two members of the same NUMBERED SERIES (#1608): folded
    forms that differ while their digit skeletons (:func:`_digit_skeleton`) are
    equal, i.e. the tags differ only in the values of their numbers.
    ``issue:#1599`` is not a misspelling of ``issue:#179``, nor ``v0.73.0`` of
    ``v0.69.0``, nor one ``session-<date>`` of another — yet all sit within 2
    edits or share a prefix, so workspaces that tag by issue, version or date
    would get a hint on almost every write. A pair whose letters or number of
    numeric fields differ is not a series and goes through the rules above
    unchanged (``isue:#1599`` / ``issue:#1599``, ``oauth`` / ``oauth2``,
    ``v0.73`` / ``v0.73.0``). Neither is a pair whose numbers are equal in value
    and differ only in zero padding (``sprint-07`` / ``sprint-7``,
    :func:`_digit_runs`): that is one identifier written two ways, which the
    fold does not unify, so it stays a suggestion.

    Args:
        requested: The tag the caller filtered on.
        candidate: A tag that exists in the context's vocabulary.

    Returns:
        True if the pair should be surfaced as a suggestion.
    """
    a, b = normalize_tag(requested), normalize_tag(candidate)
    if not a or not b or a == b:
        return bool(a) and a == b
    # Equal skeletons need a digit on both sides, so the search is only a fast
    # path that keeps the extra fold off the (far more common) unnumbered pairs.
    if (
        _DIGIT_RUN.search(a)
        and _DIGIT_RUN.search(b)
        and _digit_skeleton(requested) == _digit_skeleton(candidate)
        and _digit_runs(requested) != _digit_runs(candidate)
    ):
        return False
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


def _segments(tag: str) -> list[str]:
    """The tag's segments, each folded with :func:`normalize_tag`.

    NFKC runs first so full-width separators (``－``, ``：``) split like ASCII
    ones. Folding per segment (rather than splitting the folded whole, which
    has no separators left) is what makes ``sessions`` the generic of
    ``session-cookie`` and ``NEXT_PUBLIC`` the generic of ``next-public-plan``.
    A segment whose fold is empty (``session-.-cookie``) carries no signal and
    is dropped, so it can neither match nor break a match.
    """
    pieces = _SEGMENT_BOUNDARY.split(unicodedata.normalize("NFKC", tag))
    return [folded for folded in map(normalize_tag, pieces) if folded]


def is_specialisation(a: str, b: str) -> bool:
    """Whether one tag is a sub-topic of the other, i.e. a compound built on it.

    True when the segments of one tag are a PROPER whole-segment prefix of the
    other's: ``session-cookie`` / ``session``, ``cache-layer-redis`` /
    ``cache-layer``, ``some-repo#62`` / ``some-repo``. Such a pair is a topic
    and a sub-topic, which an author means differently — not two spellings of
    one tag — so the write lint must not call it a near-duplicate. Symmetric,
    pure, and reads no vocabulary.

    The boundary is the whole signal. :func:`is_near_duplicate` compares FOLDED
    forms, from which :func:`normalize_tag` has already removed the separators,
    so ``sessioncookie.startswith("session")`` is indistinguishable from
    ``devenvironment.startswith("devenv")``. Splitting before folding restores
    it: ``dev-env`` / ``dev-environment`` (``env`` != ``environment``),
    ``deploy-check`` / ``deploy-checklist`` and every single-segment pair
    (``oauth`` / ``oauth2``, ``kube`` / ``kubernetes``) are NOT specialisations,
    so the abbreviation case the prefix rule exists for survives. The accepted
    price is that a respelling which happens to end on a boundary — ``auth`` /
    ``auth-n``, ``front`` / ``front-end``, ``python`` / ``python-3`` — goes
    silent too: it is structurally identical to ``java`` / ``java-script``,
    which must be. The unseparated spellings (``authn``, ``frontend``,
    ``python3``) still hint.

    Never true when the whole folds are equal: ``deploy`` / ``deploy-s`` and
    ``session`` / ``session-`` are mechanical variants and stay flagged, even
    though the second has an extra segment. Checked first, before segmenting.

    Never true for two PRECISIONS of one numeric identifier: when the shorter
    tag's LAST segment ends in a digit and every extra segment is all digits
    (``session-2026-09`` / ``session-2026-09-21``, ``2026-09`` / ``2026-09-21``)
    the pair stays a near-duplicate, as the docs promise. The test is on the
    last segment only, not on any digit in the shorter tag: a bare series name
    does not end in a digit, so ``session`` / ``session-2026-09-11`` IS a
    specialisation, and so is ``s3-bucket`` / ``s3-bucket-2`` despite the ``3``.

    Args:
        a: One tag, as written.
        b: The other tag, as written.

    Returns:
        True if the pair is a topic and a sub-topic of it.
    """
    whole_a, whole_b = normalize_tag(a), normalize_tag(b)
    if not whole_a or not whole_b or whole_a == whole_b:
        return False
    segments_a, segments_b = _segments(a), _segments(b)
    if len(segments_a) == len(segments_b):
        return False  # a proper prefix needs one side to be longer
    shorter, longer = sorted((segments_a, segments_b), key=len)
    if not shorter or longer[: len(shorter)] != shorter:
        return False
    extra = longer[len(shorter) :]
    if shorter[-1][-1].isdigit() and all(segment.isdigit() for segment in extra):
        return False  # one numeric identifier at two precisions, not a sub-topic
    return True
