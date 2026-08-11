"""Durability contract surfaced on every memory write (Issue #1505).

``remember()`` returns ``scope: "working"``, which reads as "not saved yet" to a
writing agent — especially next to ``delivery_mode="always"``, which pins
straight to persistent. Nothing in the response said what working actually
implies, so a caller storing a high-importance result had no way to tell whether
it survives until consolidation runs.

What this module states, all read off the code that enforces it:

* The row is committed to PostgreSQL before ``remember()`` returns. ``scope``
  does not gate durability at all — it selects which consolidation lifecycle the
  memory is on.
* Which consolidation pass, if any, this deployment actually runs. The two are
  mutually exclusive and neither is guaranteed to be on:

  - ``SLEEP_ENABLED=true`` + ``ENABLE_NEURAL_MEMORY=true`` -> the sleep pass
    (``services/sleep/consolidation.py``). Promotes on adoption (a deliberate
    ``reference()``), importance once aged, or graph centrality.
  - ``SLEEP_ENABLED=true`` + ``ENABLE_NEURAL_MEMORY=false`` -> **neither**:
    ``sleep_maintenance_task`` returns at its neural-memory guard, and
    ``schedule_neural_tasks`` skips registering the legacy cron whenever sleep
    is on. Nothing promotes or archives.
  - ``SLEEP_ENABLED`` unset/false -> the legacy pass
    (``tasks/neural_tasks.py::consolidation_task``, registered unconditionally
    by ``schedule_neural_tasks``, which ``api/main.py`` always calls). Promotes
    on ``access_count`` (surfacing, not adoption) or importance once aged; it
    has no centrality criterion.

* The consolidation archival floor. Derived from whichever path is running so
  the number can never describe a pass this deployment does not have.

DELIBERATELY NOT CLAIMED — this is not a retention SLA:

* Near-duplicate merge (``services/sleep/dedup_merge.py``) soft-deletes the
  loser of a >= 0.98-cosine pair with **no age, scope, or adoption gate**;
  ``_fetch_active_memories`` selects on user/workspace/context and
  ``deleted_at IS NULL`` only. A memory written minutes ago can lose a merge the
  same night (its tags and edges transfer to the winner, but its id stops
  resolving). Any global "nothing you write today can be removed" wording would
  therefore be false, which is why the age field is named for consolidation and
  the detail text scopes itself.
* ``forget()`` and account erasure remove memories on demand.
"""

from __future__ import annotations

import os
from functools import lru_cache

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


@lru_cache(maxsize=1)
def _sleep_archive_min_age_days() -> int:
    """Archival age floor enforced by the sleep consolidation pass.

    Imported lazily: ``services.sleep.consolidation`` pulls in Qdrant, the graph
    service and the LLM service, none of which a write path should drag in at
    import time.
    """
    from services.sleep.consolidation import ARCHIVE_MIN_AGE_DAYS

    return ARCHIVE_MIN_AGE_DAYS


@lru_cache(maxsize=1)
def _legacy_archive_min_age_days() -> int:
    """Archival age floor enforced by the legacy consolidation task."""
    from tasks.neural_tasks import LEGACY_ARCHIVE_MIN_AGE_DAYS

    return LEGACY_ARCHIVE_MIN_AGE_DAYS


def consolidation_archive_min_age_days() -> int | None:
    """The running pass's archival floor, or None when no pass runs."""
    pass_name = active_consolidation_pass()
    if pass_name == SLEEP_CONSOLIDATION:
        return _sleep_archive_min_age_days()
    if pass_name == LEGACY_CONSOLIDATION:
        return _legacy_archive_min_age_days()
    return None


_PROMOTION_CRITERIA = {
    SLEEP_CONSOLIDATION: (
        "adoption via reference(), high importance once aged, or graph centrality"
    ),
    LEGACY_CONSOLIDATION: "repeat access, or high importance once aged",
}


def persistence_info(scope: str) -> PersistenceInfo | None:
    """Build the durability block for a just-written memory.

    Args:
        scope: The memory's scope as persisted ("working" or "persistent").

    Returns:
        PersistenceInfo describing what that scope implies for durability, or
        None when it cannot be built. This block is advisory and is computed
        AFTER the write has committed, so it must be total: an unrecognized
        scope, or any failure below, omits the field rather than raising.

    Note:
        The blanket guard is not defensive padding. The archival floor is read
        through a lazy import of ``services.sleep.consolidation``, which pulls
        in Qdrant, the graph service and the LLM service — the first write in a
        process pays that import, and anything wrong in that chain would
        otherwise surface as a failed ``remember()`` for a memory that is
        already stored, prompting the caller to retry and duplicate it.
    """
    try:
        return _persistence_info(scope)
    except Exception as e:  # noqa: BLE001 — advisory; never fail a committed write
        logger.warning("persistence_info_failed", scope=scope, error=str(e))
        return None


def _persistence_info(scope: str) -> PersistenceInfo | None:
    """Build the block, or None for an unrecognized scope. May raise."""
    if scope == "persistent":
        return PersistenceInfo(
            scope="persistent",
            committed=True,
            promotes_via=None,
            consolidation_archive_min_age_days=None,
            detail=(
                "Committed and persistent. Consolidation acts only on "
                "working-scope memories, so it will not archive this one. "
                "Separate maintenance (near-duplicate merge) and an explicit "
                "forget() are not scope-gated and still apply."
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
                "deployment, so it will stay working-scope and consolidation "
                "will not archive it."
            ),
        )

    days = consolidation_archive_min_age_days()
    return PersistenceInfo(
        scope="working",
        committed=True,
        promotes_via=pass_name,
        consolidation_archive_min_age_days=days,
        detail=(
            "Committed and durable now — 'working' is a lifecycle label, not a "
            f"staging buffer. Promotes to persistent via {pass_name} "
            f"({_PROMOTION_CRITERIA[pass_name]}). That pass will not archive it "
            f"before {days} days old, and only with zero adoption. Separate "
            "maintenance (near-duplicate merge) and an explicit forget() are "
            "not bound by that floor."
        ),
    )
