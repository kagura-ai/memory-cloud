"""Per-key provenance of a memory's ``details``, replayed from its revision log (#2275).

Every write of ``memories.details`` replaces the whole dict, and the
``updated`` row it leaves in ``memory_revisions`` holds the whole dict it
overwrote in ``prior.details`` (``memory_versions.changed_prior``). So the
details edits of one memory, ascending by ``(version, id)``, give the whole
sequence of states it held: ``S_0`` (the prior of the first edit: the
creation value, NULL = no keys) through ``S_n`` (the live value), where the
edit ``e_i`` moved ``S_i`` to ``S_{i+1}``. Who introduced the value each
top-level key holds in each state follows from consecutive states and each
edit's actor and writer, which is what an erasure needs: the keys an erased
member wrote go back, the keys a surviving member wrote — or carried
unchanged in a whole-dict edit of their own — are theirs or the earlier
writer's, never the erased member's to lose.

Pure: no database, no ORM. ``plan_details_erasure`` takes the rows
``erasure_details._updated_rows`` reads under the memory locks and the live
``details`` and author of each candidate memory, and returns the keys to put
back and the revision priors to strip (``erasure_details`` writes them).

Rules, per memory (``replay``):

- Grain: top-level keys. A nested object (``trigger``, ``location``, ...) is
  one value: whoever rewrote any part of it wrote all of it. Every write path
  validates those objects whole, so reverting one part could leave an invalid
  one; a member's sub-value inside an object a later member rewrote stays
  (documented in ``docs/mcp-tools.md``). Values compare with ``==`` on the
  decoded JSON (dicts order-insensitive, lists ordered).
- ``S_0``'s keys are the author's (``Memory.user_id``). A key unchanged from
  one state to the next keeps its writer (carried). A key an ``api`` edit
  changed or added is that edit's actor's.
- A put-back row (``writer`` ``rollback`` or ``erasure``) writes no value of
  its own: a key it changed is attributed to whoever wrote that value below
  — the state right below the version it names in ``restored_from``
  (``pre(r)``) when that state holds the value, else the newest earlier state
  holding the same value (rows without ``restored_from``: session rollbacks
  from before #2266; exact unless the rolled-back session itself re-wrote a
  key to an earlier value), else nobody (a value the put-back synthesized:
  the trigger window an erasure merges into a time memory). An ``erasure``
  row written since #2275 holds the overwritten dict minus the erased
  member's keys, so the keys it carried keep their writers.
- A state an earlier erasure stripped whole (its prior lists ``details``
  under ``erased``: every erasure before v0.129.0 stripped the whole value,
  the authored arm still does) is UNKNOWN. The next known state is compared
  with the newest known state below it; a key unchanged since keeps that
  writer, any other is the next edit's (or attributed as a put-back). When
  that edit is a surviving member's ``api`` edit and the stripped states in
  between may hold the subject's value (written by the subject, or by a
  put-back row), the key is **ambiguous**: it may be the subject's value
  carried through, or the survivor's own change, and the stripped state
  cannot tell. Ambiguous keys are kept and counted, never reverted: a
  survivor's write is never destroyed on a guess.
- **The subject's keys** are the live keys whose writer is the subject.
  Each goes back to the value it had right below the edit that wrote it,
  walking further down past every value an erased member (the subject or
  one of ``erased_actors``) wrote — including a value an earlier erasure
  stripped from a prior per key (``erased_keys``) — or is removed when the
  key was absent there. Where that state is UNKNOWN and the edit that wrote
  it is an erased member's, the walk goes on below it; otherwise the value
  is unavailable and the key is removed. Keys an erased member other than
  the subject wrote are left to that member's erasure (or re-run).
- **Deletions stand**: a key that was removed — by the subject or by anyone
  else — is not put back, even when the subject's edit is the newest; a
  walk that reaches a state without the key stops there.
- **Strip**: every intermediate state's keys whose writer is the subject
  leave the prior that holds the state; the rest of the dict stays. The
  prior lists the removed keys' digests under ``erased_details_keys``
  (``details_key_digest``; never a key name): a later walk passes a
  stripped key like the erased member's value it was, and does not take it
  for a removal. Readers of the version history ignore the list (a rebuilt
  version shows the other members' keys, nothing unavailable). With it the
  result does not depend on which of two erasures runs first.

A re-run over the result plans nothing: no state, and no live key, is
attributed to the subject any more.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from models.memory import REVISION_WRITER_ERASURE, REVISION_WRITER_ROLLBACK
from utils.hashing import sha256_hex

_PUT_BACK: Final = (REVISION_WRITER_ERASURE, REVISION_WRITER_ROLLBACK)


def details_key_digest(key: str) -> str:
    """How a prior lists a ``details`` key an erasure removed from it
    (``PRIOR_ERASED_DETAILS_KEYS_KEY``): the key's sha256, never its name."""
    return sha256_hex(key)


@dataclass(frozen=True)
class DetailsEdit:
    """One ``updated`` revision of a memory's log (``erasure_details._updated_rows``).

    Attributes:
        memory_id / version / id: The row's position in its memory's log.
        actor_user_id: Who wrote the edit (raw sub, or a pseudonym once erased).
        writer: One of ``REVISION_WRITERS``.
        details: ``prior.details`` — the value the edit overwrote (None when
            it was NULL, when an erasure stripped it, or when the edit did not
            change ``details``).
        erased: An earlier erasure stripped ``prior.details`` whole (it lists
            ``details`` under ``PRIOR_ERASED_KEY``): the value is unavailable.
        erased_keys: The digests of the ``details`` keys an erasure removed
            from this prior per key (``PRIOR_ERASED_DETAILS_KEYS_KEY``, #2275).
        restored_from: On a put-back row (``erasure``, and ``rollback`` since
            #2266), ``prior.restored_from``: the lowest version at which a
            value it put back over was written.
        details_changed: The edit changed ``details``
            (``revision_edited_details``). Rows that did not are ignored.
    """

    memory_id: UUID
    version: int
    id: int
    actor_user_id: str
    writer: str
    details: Any
    erased: bool
    erased_keys: frozenset[str] = frozenset()
    restored_from: int | None = None
    details_changed: bool = True


class _Unknown:
    """A state an earlier erasure stripped whole."""

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN: Final = _Unknown()


@dataclass(frozen=True)
class Origin:
    """Who introduced the value a key holds in a state.

    Attributes:
        actor: The member who wrote it; None when nobody did (a value a
            put-back row synthesized). Never reverted, never stripped.
        version: The version of the edit that wrote it (1 for ``S_0``).
        state: The index of the first state holding it (0 for ``S_0``).
        ambiguous: Written by a surviving member's edit right above a
            stripped state that may hold the subject's value.
    """

    actor: str | None
    version: int
    state: int
    ambiguous: bool = False


@dataclass(frozen=True)
class Replay:
    """The states of one memory's ``details`` and their writer maps.

    ``states[i]`` is ``S_i`` (``UNKNOWN`` when stripped whole), ``writers[i]``
    maps each key of ``S_i`` to its ``Origin`` (None when ``S_i`` is
    ``UNKNOWN``), ``edits[i]`` moved ``S_i`` to ``S_{i+1}``; the last state is
    the live value. ``stripped[i]`` are the digests of the keys an erasure
    removed from ``S_i`` (an erased member's values, not removals);
    ``versions[i]`` is ``edits[i].version`` (ascending, for ``_pre``).
    """

    edits: tuple[DetailsEdit, ...]
    states: tuple[Any, ...]
    writers: tuple[dict[str, Origin] | None, ...]
    stripped: tuple[frozenset[str], ...]
    versions: tuple[int, ...]


@dataclass(frozen=True)
class KeyRevert:
    """One live key of the subject's and where it goes back to.

    Attributes:
        key: The top-level key.
        target: The value to put back (None when ``present`` is False).
        present: The key had a value below the subject's write; False
            removes it.
        unavailable: That value was stripped by an earlier erasure: the key
            is removed.
        from_version: The version of the lowest edit the walk went past (the
            edit right above the state the value comes from).
    """

    key: str
    target: Any
    present: bool
    unavailable: bool
    from_version: int


@dataclass(frozen=True)
class MemoryRevert:
    """What the erasure writes on one memory's live ``details``.

    Attributes:
        memory_id: The memory.
        reverts: The subject's live keys, in key order.
        from_version: The lowest ``KeyRevert.from_version``: the erasure
            row's ``restored_from``.
        result: The live value with the reverts applied (NULL when nothing is
            left and the value below was NULL or unavailable, else ``{}``).
        prior: The live value minus the subject's keys: the erasure row's
            ``prior.details``, stripped like every other prior.
        erased_keys: The digests of the subject's keys: the erasure row's
            ``PRIOR_ERASED_DETAILS_KEYS_KEY``.
    """

    memory_id: UUID
    reverts: tuple[KeyRevert, ...]
    from_version: int
    result: Any
    prior: dict[str, Any]
    erased_keys: tuple[str, ...] = ()

    @property
    def unavailable(self) -> bool:
        """A key's value to go back to was stripped by an earlier erasure."""
        return any(r.unavailable for r in self.reverts)


@dataclass(frozen=True)
class PriorStrip:
    """The subject's keys to remove from one revision row's ``prior.details``."""

    row_id: int
    keys: tuple[str, ...]


@dataclass(frozen=True)
class DetailsErasurePlan:
    """What an erasure of one subject does to ``details`` (``plan_details_erasure``).

    Attributes:
        reverts: Memories whose live ``details`` holds a key of the subject's,
            in memory id order.
        strips: Revision rows whose prior holds a key of the subject's, in
            row id order.
        keys_ambiguous: Live keys kept because a stripped state below them
            may hold the subject's value (never reverted).
        memory_ids: The candidate memories replayed, in id order.
    """

    reverts: tuple[MemoryRevert, ...] = ()
    strips: tuple[PriorStrip, ...] = ()
    keys_ambiguous: int = 0
    memory_ids: tuple[UUID, ...] = ()


def _keys(state: Any) -> Mapping[str, Any]:
    """The top-level keys of a known state (none for NULL or a non-object)."""
    return state if isinstance(state, dict) else {}


def _holds(state: Any, key: str, value: Any) -> bool:
    keys = _keys(state)
    return key in keys and keys[key] == value


def _pre(versions: Sequence[int], restored_from: int, i: int) -> int:
    """``pre(r)``: the state right below the first details edit at or above
    version ``restored_from`` (at most ``i``). ``versions`` ascend."""
    return min(bisect_left(versions, restored_from), i)


def _may_hold_subjects(
    states: Sequence[Any], edits: Sequence[DetailsEdit], i: int, subject: str
) -> bool:
    """The stripped states right at and below ``i`` may hold a value of the
    subject's: one of them was written by the subject, or by a put-back row
    above a details edit of the subject's (a put-back puts back older values
    only, so without one below it holds none of theirs)."""
    j = i
    while j >= 1 and states[j] is UNKNOWN:
        producer = edits[j - 1]
        if producer.actor_user_id == subject:
            return True
        if producer.writer in _PUT_BACK and any(
            e.actor_user_id == subject and e.writer not in _PUT_BACK for e in edits[: j - 1]
        ):
            return True
        j -= 1
    return False


def replay(
    edits: Sequence[DetailsEdit],
    live: Any,
    author: str,
    *,
    subject: str,
    erased: Collection[str],
) -> Replay:
    """Replay one memory's details edits into per-key writer maps.

    Args:
        edits: The memory's details edits, ascending by ``(version, id)``.
        live: The live ``details``.
        author: ``Memory.user_id``: the writer of ``S_0``'s keys.
        subject: The member being erased (for the ambiguity test).
        erased: The subject and every other erased member.

    Returns:
        The states and their writer maps (see the module docstring).
    """
    states: list[Any] = [UNKNOWN if e.erased else e.details for e in edits]
    states.append(live)
    writers: list[dict[str, Origin] | None] = [None] * len(states)
    versions = [e.version for e in edits]
    last: int | None = None  # the newest known state at or below i
    if states[0] is not UNKNOWN:
        writers[0] = {k: Origin(author, 1, 0) for k in _keys(states[0])}
        last = 0
    for i, edit in enumerate(edits):
        if states[i] is not UNKNOWN:
            last = i
        after = states[i + 1]
        if after is UNKNOWN:
            continue
        before_known = states[i] is not UNKNOWN
        put_back = edit.writer in _PUT_BACK
        pre = (
            _pre(versions, edit.restored_from, i)
            if put_back and edit.restored_from is not None
            else None
        )
        # an erasure row since #2275 carries the overwritten dict minus the
        # erased keys: what it did not revert keeps its writer first. Any
        # other put-back puts back pre(r) whole.
        carried_first = edit.writer == REVISION_WRITER_ERASURE and before_known
        ambiguous = (
            not put_back
            and not before_known
            and edit.actor_user_id not in erased
            and _may_hold_subjects(states, edits, i, subject)
        )
        mapping: dict[str, Origin] = {}
        for key, value in _keys(after).items():
            mapping[key] = _attribute(
                key,
                value,
                states,
                writers,
                edit,
                i,
                last=last,
                pre=pre,
                carried_first=carried_first,
                ambiguous=ambiguous,
            )
        writers[i + 1] = mapping
    stripped = (*(e.erased_keys for e in edits), frozenset())
    return Replay(tuple(edits), tuple(states), tuple(writers), stripped, tuple(versions))


def _attribute(
    key: str,
    value: Any,
    states: Sequence[Any],
    writers: Sequence[dict[str, Origin] | None],
    edit: DetailsEdit,
    i: int,
    *,
    last: int | None,
    pre: int | None,
    carried_first: bool,
    ambiguous: bool,
) -> Origin:
    """The writer of ``key = value`` in ``S_{i+1}`` (``replay``'s rules)."""

    def from_state(j: int | None) -> Origin | None:
        if j is None or not _holds(states[j], key, value):
            return None
        mapping = writers[j]
        return mapping.get(key) if mapping is not None else None

    if edit.writer not in _PUT_BACK:
        carried = from_state(last)
        if carried is not None:
            return carried
        return Origin(edit.actor_user_id, edit.version, i + 1, ambiguous=ambiguous)
    order = (last, pre) if carried_first else (pre, last)
    for j in order:
        found = from_state(j)
        if found is not None:
            return found
    for j in range(i, -1, -1):  # the newest earlier state holding the value
        found = from_state(j)
        if found is not None:
            return found
    return Origin(None, edit.version, i + 1)


def _revert(
    key: str, origin: Origin, run: Replay, erased: Collection[str]
) -> tuple[KeyRevert, int]:
    """Where the subject's live ``key`` goes back to: below the edit that
    wrote it, past every value an erased member wrote.

    A key an erasure stripped from a state is walked past like the erased
    member's value it was; a state without the key otherwise ends the walk
    (the key is removed: deletions stand). A state an erasure before
    v0.129.0 stripped whole is walked past when an erased member's own
    (``api``) edit wrote it.

    Returns:
        The revert, and the index of the state its value comes from.
    """
    states, writers, edits = run.states, run.writers, run.edits
    digest = details_key_digest(key)

    def erased_wrote(j: int) -> bool:
        return (
            j >= 1
            and edits[j - 1].writer not in _PUT_BACK
            and (edits[j - 1].actor_user_id in erased)
        )

    j = origin.state - 1
    while True:
        if states[j] is UNKNOWN:
            if not erased_wrote(j):
                # stripped, and not by an erased member's own edit: gone
                return KeyRevert(key, None, False, True, edits[j].version), j
            j -= 1
            continue
        mapping = writers[j] or {}
        below = mapping.get(key)
        if below is not None and below.actor in erased:
            if below.state == 0:
                # the creation value of an erased author: nothing to go back to
                return KeyRevert(key, None, False, False, edits[j].version), j
            j = below.state - 1
            continue
        if below is None and j >= 1 and digest in run.stripped[j]:
            j = _below_stripped(run, j, key, digest)  # an erased member's value
            continue
        keys = _keys(states[j])
        present = key in keys
        revert = KeyRevert(key, keys[key] if present else None, present, False, edits[j].version)
        return revert, j


def _below_stripped(run: Replay, j: int, key: str, digest: str) -> int:
    """The state the stripped value of ``key`` in ``S_j`` came from.

    The edit that wrote ``S_j`` carried it from ``S_{j-1}``, or wrote it
    itself (an erased member's ``api`` edit): ``S_{j-1}`` either way. A
    put-back row put it back from ``pre(r)`` — a session rollback whole, an
    erasure row for the keys it reverted (absent from its own prior) — so
    the walk goes on there, not in the state the put-back replaced.
    """
    producer = run.edits[j - 1]
    if producer.writer not in _PUT_BACK or producer.restored_from is None:
        return j - 1
    if producer.writer == REVISION_WRITER_ERASURE:
        prior = run.states[j - 1]
        if prior is UNKNOWN or key in _keys(prior) or digest in run.stripped[j - 1]:
            return j - 1  # carried by the erasure row
    return _pre(run.versions, producer.restored_from, j - 1)


def _plan_memory(
    memory_id: UUID,
    edits: Sequence[DetailsEdit],
    live: Any,
    author: str,
    subject: str,
    erased: Collection[str],
) -> tuple[MemoryRevert | None, list[PriorStrip], int]:
    run = replay(edits, live, author, subject=subject, erased=erased)
    strips = []
    for i, edit in enumerate(edits):
        mapping = run.writers[i]
        if mapping is None:
            continue
        keys = tuple(sorted(k for k, o in mapping.items() if o.actor == subject))
        if keys:
            strips.append(PriorStrip(edit.id, keys))
    live_writers = run.writers[-1] or {}
    ambiguous = sum(1 for o in live_writers.values() if o.ambiguous)
    mine = sorted(k for k, o in live_writers.items() if o.actor == subject)
    if not mine:
        return None, strips, ambiguous
    walked = [_revert(k, live_writers[k], run, erased) for k in mine]
    reverts = tuple(r for r, _ in walked)
    result: dict[str, Any] = dict(_keys(live))
    for r in reverts:
        if r.present:
            result[r.key] = r.target
        else:
            result.pop(r.key, None)
    lowest, below = min(walked, key=lambda w: (w[0].from_version, w[1]))
    value: Any = result
    if not result and (lowest.unavailable or run.states[below] is None):
        value = None  # nothing left, and the value below was NULL (or gone)
    prior = {k: v for k, v in _keys(live).items() if k not in mine}
    return (
        MemoryRevert(
            memory_id,
            reverts,
            lowest.from_version,
            value,
            prior,
            tuple(sorted(details_key_digest(k) for k in mine)),
        ),
        strips,
        ambiguous,
    )


def plan_details_erasure(
    rows: Iterable[DetailsEdit],
    live: Mapping[UUID, tuple[Any, str]],
    subject: str,
    *,
    erased_actors: Collection[str] = frozenset(),
) -> DetailsErasurePlan:
    """The per-key restore and strip of ``subject``'s ``details`` (#2275).

    Pure, deterministic, insensitive to the order of ``rows``.

    Args:
        rows: The ``updated`` rows of the candidate memories, in any order;
            rows that changed no ``details`` are ignored.
        live: Each candidate memory's ``(details, author)``. A memory absent
            here (gone) is skipped; so is one the subject authored (the
            authored arm strips it whole).
        subject: The member being erased (raw sub, or the pseudonym on a
            re-run).
        erased_actors: Other erased members' pseudonyms: their values are
            walked past, never reverted here.

    Returns:
        The reverts (memory id order), the strips (row id order) and the
        ambiguous key count.
    """
    by_memory: dict[UUID, list[DetailsEdit]] = defaultdict(list)
    for row in rows:
        if row.details_changed:
            by_memory[row.memory_id].append(row)
    erased = frozenset({subject, *erased_actors})
    reverts: list[MemoryRevert] = []
    strips: list[PriorStrip] = []
    ambiguous = 0
    replayed: list[UUID] = []
    for memory_id in sorted(by_memory):
        if memory_id not in live:
            continue
        details, author = live[memory_id]
        if author == subject:
            continue
        replayed.append(memory_id)
        edits = sorted(by_memory[memory_id], key=lambda e: (e.version, e.id))
        revert, memory_strips, memory_ambiguous = _plan_memory(
            memory_id, edits, details, author, subject, erased
        )
        if revert is not None:
            reverts.append(revert)
        strips.extend(memory_strips)
        ambiguous += memory_ambiguous
    return DetailsErasurePlan(
        reverts=tuple(reverts),
        strips=tuple(sorted(strips, key=lambda s: s.row_id)),
        keys_ambiguous=ambiguous,
        memory_ids=tuple(replayed),
    )
