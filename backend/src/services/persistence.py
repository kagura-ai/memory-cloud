"""Durability contract surfaced on every memory write (Issue #1505).

``remember()`` returns ``scope: "working"``, which reads as "not saved yet" to a
writing agent — especially next to ``delivery_mode="always"``, which pins
straight to persistent. Nothing in the response said what working actually
implies, so a caller storing a high-importance result had no way to tell whether
it survives until consolidation runs.

What this module states, all read off the code that enforces it:

* The row is committed to PostgreSQL before ``remember()`` returns. ``scope``
  does not gate durability at all: ``persistent`` means used, or old enough by
  rule; ``working`` is everything else. It has no effect on ranking, quota or
  retention.
* A memory is kept until its owner or writer deletes it (#3021). No pass
  archives or deletes a memory for age or disuse, so
  ``consolidation_archive_min_age_days`` is always null (the key stays so the
  reply keeps its shape).
* Which consolidation pass, if any, this deployment runs to promote working
  memories. The two are mutually exclusive and neither is guaranteed to be on:

  - ``SLEEP_ENABLED=true`` + ``ENABLE_NEURAL_MEMORY=true`` -> the sleep pass
    (``services/sleep/consolidation.py``). Promotes on adoption (a deliberate
    ``reference()``), importance once aged, or graph centrality.
  - ``SLEEP_ENABLED=true`` + ``ENABLE_NEURAL_MEMORY=false`` -> **neither**:
    ``sleep_maintenance_task`` returns at its neural-memory guard, and
    ``schedule_neural_tasks`` skips registering the legacy cron whenever sleep
    is on. Nothing promotes.
  - ``SLEEP_ENABLED`` unset/false -> the legacy pass
    (``tasks/neural_tasks.py::consolidation_task``, registered unconditionally
    by ``schedule_neural_tasks``, which ``api/main.py`` always calls). Promotes
    on ``access_count`` (surfacing, not adoption) or importance once aged; it
    has no centrality criterion.

DELIBERATELY NOT CLAIMED — this is not a retention SLA:

* Near-duplicate merge (``services/sleep/dedup_merge.py``), when a deployment
  enables it, soft-deletes the loser of a pair with **no age, scope, or
  adoption gate**: a pair at or above ``SLEEP_DEDUP_SIMILARITY_THRESHOLD``
  cosine (default 0.92) that the LLM judge calls a duplicate, or, when no judge
  runs, a pair at or above 0.98 (``AUTO_MERGE_THRESHOLD``).
  ``_fetch_active_memories`` selects on user/workspace/context,
  ``deleted_at IS NULL`` and the lane exemptions only
  (``delivery_mode='always'`` rows never enter candidacy, #1519, and since
  #1523 the same predicate exempts them from importance re-evaluation too —
  ``pinned=True`` below carries that promise; ``type='time'`` rows never do
  either, #1524, since the Time Memory lane serves them by trigger window, not
  summary; tool guardrails; and, since #2996, rows the resource indexer owns,
  which are persistent from creation and whose lifecycle belongs to their
  source). An unpinned, non-time memory written minutes ago can lose a merge
  the same night (its tags and edges transfer to the winner, but its id stops
  resolving), which is why the detail text names the merge next to forget().
* ``forget()`` and account erasure remove memories on demand.
"""

from __future__ import annotations

import os

from models.schemas import PersistenceInfo
from utils.logger import get_logger

logger = get_logger(__name__)

SLEEP_CONSOLIDATION = "sleep_consolidation"
LEGACY_CONSOLIDATION = "legacy_consolidation"


def _env_true(name: str) -> bool:
    """Match how the task modules themselves read these flags."""
    return os.getenv(name, "false").lower() == "true"


def active_consolidation_pass() -> str | None:
    """Which consolidation pass this deployment runs, or None if neither.

    Mirrors the guards in ``tasks/sleep_tasks.py::sleep_maintenance_task`` and
    ``tasks/neural_tasks.py::schedule_neural_tasks``. Not cached: the flags are
    read per call so a process that is reconfigured and restarted never reports
    a stale pass, and ``os.getenv`` is far cheaper than the write it rides on.
    """
    if _env_true("SLEEP_ENABLED"):
        # schedule_neural_tasks skips the legacy cron whenever sleep is on, so
        # the sleep pass's own neural-memory guard decides whether ANY pass runs.
        return SLEEP_CONSOLIDATION if _env_true("ENABLE_NEURAL_MEMORY") else None
    return LEGACY_CONSOLIDATION


_PROMOTION_CRITERIA = {
    SLEEP_CONSOLIDATION: (
        "adoption via reference(), high importance once aged, or graph centrality"
    ),
    LEGACY_CONSOLIDATION: "repeat access, or high importance once aged",
}


def persistence_info(
    scope: str, *, pinned: bool = False, tool_triggered: bool = False
) -> PersistenceInfo | None:
    """Build the durability block for a just-written memory.

    Args:
        scope: The memory's scope as persisted ("working" or "persistent").
        pinned: True when the memory is ``delivery_mode='always'`` (#1519).
            Pinned rows never enter near-duplicate merge candidacy, so the
            merge caveat is replaced by that promise instead of repeated.
        tool_triggered: True when the memory carries ``details.tool_trigger``
            (a tool guardrail). Sleep maintenance excludes it the same way it
            excludes a pinned row, so the block says so — the generic merge
            caveat would be false for it.

    Returns:
        PersistenceInfo describing what that scope implies for durability, or
        None when it cannot be built. This block is advisory and is computed
        AFTER the write has committed, so it must be total: an unrecognized
        scope, or any failure below, omits the field rather than raising —
        a failed ``remember()`` for a memory that is already stored would
        prompt the caller to retry and duplicate it.
    """
    try:
        return _persistence_info(scope, pinned=pinned, tool_triggered=tool_triggered)
    except Exception as e:  # noqa: BLE001 — advisory; never fail a committed write
        logger.warning("persistence_info_failed", scope=scope, error=str(e))
        return None


# #3021: the rule every block states, whatever the scope or the pass.
_KEPT = "It is kept until deleted: nothing removes a memory for age or disuse."


def _merge_caveat(pinned: bool, *, tool_triggered: bool = False) -> str:
    """The sentence about the removals that remain.

    Pinned memories (#1519, #1523) and tool guardrails (``details.tool_trigger``)
    are excluded from near-duplicate merge and importance re-evaluation at
    their candidate fetch (one shared predicate each), so for them the merge
    caveat becomes a promise and only forget() remains.
    """
    if pinned or tool_triggered:
        # Hedged on purpose: the exemption holds only while the pin / the
        # marking holds (an unpin or unmark re-enters candidacy), and forget()
        # is not the only removal path (deleting the context removes it too) —
        # so no "never", no "only".
        if pinned and tool_triggered:
            hold = (
                "While it stays pinned (delivery_mode='always') or keeps its details.tool_trigger"
            )
        elif pinned:
            hold = "While it stays pinned (delivery_mode='always')"
        else:
            hold = "While it keeps its details.tool_trigger (a tool guardrail)"
        # Rollback: a pin also refuses the demote of a promotion (#1523); a
        # guardrail is not scope-bound, so only its importance is protected.
        rollback = "never demotes it" if pinned else "leaves its importance alone"
        return (
            f"{hold}, Sleep maintenance does not select it — near-duplicate "
            "merge and importance re-evaluation both exclude it, and rollback "
            f"{rollback}; an explicit forget() still removes it."
        )
    return (
        "Separate maintenance (near-duplicate merge) and an explicit "
        "forget() are not scope-gated and still apply."
    )


def _persistence_info(
    scope: str, pinned: bool = False, tool_triggered: bool = False
) -> PersistenceInfo | None:
    """Build the block, or None for an unrecognized scope. May raise."""
    protected = pinned or tool_triggered
    if scope == "persistent":
        return PersistenceInfo(
            scope="persistent",
            committed=True,
            promotes_via=None,
            consolidation_archive_min_age_days=None,
            detail=(
                f"Committed and persistent. {_KEPT} "
                + _merge_caveat(pinned, tool_triggered=tool_triggered)
            ),
        )
    if scope != "working":
        logger.warning("persistence_info_unknown_scope", scope=scope)
        return None

    pass_name = active_consolidation_pass()
    if pass_name is None:
        return PersistenceInfo(
            scope="working",
            committed=True,
            promotes_via=None,
            consolidation_archive_min_age_days=None,
            detail=(
                "Committed and durable now — 'working' is a lifecycle label, "
                "not a staging buffer. No consolidation pass is enabled on this "
                f"deployment, so it will stay working-scope. {_KEPT}"
                + (" " + _merge_caveat(pinned, tool_triggered=tool_triggered) if protected else "")
            ),
        )

    return PersistenceInfo(
        scope="working",
        committed=True,
        promotes_via=pass_name,
        # #3021: no pass archives, so there is no age floor to state.
        consolidation_archive_min_age_days=None,
        detail=(
            "Committed and durable now — 'working' is a lifecycle label, not a "
            f"staging buffer. Promotes to persistent via {pass_name} "
            f"({_PROMOTION_CRITERIA[pass_name]}). {_KEPT} "
            + _merge_caveat(pinned, tool_triggered=tool_triggered)
        ),
    )
