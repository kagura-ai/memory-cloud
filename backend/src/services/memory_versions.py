"""Memory versions and the revision log (#2043).

``Memory.version`` starts at 1 and moves only when an edit changes one of
``VERSIONED_FIELDS``; each such edit writes one ``memory_revisions`` row whose
``prior`` holds the overwritten values of the changed fields. ``remember`` /
``forget`` write ``created`` / ``forgotten`` rows. These helpers are the one
place the edit paths (``update_memory`` in place, REST PATCH) and the write
paths build those rows, so the rules cannot drift between them.

Pure apart from ``record_revision`` adding a row to the caller's session; the
caller owns the transaction (the row commits with the write it describes).
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import (
    REVISION_KIND_UPDATED,
    REVISION_WRITER_API,
    VERSIONED_FIELDS,
    MemoryRevision,
)
from services.write_provenance import WriteProvenance, current_provenance
from utils.exceptions import VersionConflictError


def versioned_snapshot(memory: Any) -> dict[str, Any]:
    """The versioned fields as stored, before an edit touches them.

    ``details`` and ``tags`` are copied: an edit path may mutate the stored
    object in place (the time-trigger normalization rewrites ``details``), and
    the snapshot must keep the old value.

    Args:
        memory: The locked row about to be edited.

    Returns:
        Field name → stored value, for every name in ``VERSIONED_FIELDS``.
    """
    snapshot: dict[str, Any] = {}
    for field in VERSIONED_FIELDS:
        value = getattr(memory, field)
        snapshot[field] = copy.deepcopy(value) if isinstance(value, dict | list) else value
    return snapshot


def _comparable(field: str, value: Any) -> Any:
    # A NULL tag array and an empty one say the same thing; replacing one with
    # the other is not a new version.
    if field == "tags":
        return list(value or [])
    return value


def changed_prior(before: dict[str, Any], memory: Any) -> dict[str, Any]:
    """The overwritten values of the versioned fields this edit changed.

    Compares the row after the edit (normalization included) with
    ``before``. Only changed keys are kept; a value that was NULL is kept as
    ``None`` (JSON ``null``), so "was null" stays distinct from "unchanged"
    (an absent key).

    Args:
        before: ``versioned_snapshot`` taken before the edit.
        memory: The row with the edit applied.

    Returns:
        ``{field: old value}`` for each changed field; empty when the edit
        changed none (no version bump, no revision).
    """
    return {
        field: before[field]
        for field in VERSIONED_FIELDS
        if _comparable(field, before[field]) != _comparable(field, getattr(memory, field))
    }


def check_expected_version(memory: Any, expected_version: int | None) -> None:
    """Refuse an edit whose ``expected_version`` is no longer the row's.

    Called on the row read under ``FOR UPDATE`` and before any field is
    applied, so a refused edit writes nothing and two concurrent edits with
    the same ``expected_version`` serialize: the second reads the first's
    bumped version and is refused.

    Raises:
        VersionConflictError: ``expected_version`` is set and differs.
    """
    if expected_version is not None and memory.version != expected_version:
        raise VersionConflictError(
            memory_id=memory.id,
            expected_version=expected_version,
            current_version=memory.version,
        )


def revision_values(
    *,
    memory_id: UUID,
    context_id: UUID,
    version: int,
    kind: str,
    actor_user_id: str,
    at: datetime | None = None,
    prior: dict[str, Any] | None = None,
    writer: str = REVISION_WRITER_API,
    provenance: WriteProvenance | None = None,
) -> dict[str, Any]:
    """The column values of one revision row — the single mapping both the
    per-row writer (``record_revision``) and the bulk forget INSERT use, so a
    column added here (#2044's provenance) reaches every write path.

    ``at`` is left out when None, so the column default applies.
    ``provenance`` defaults to the current request's (#2044).
    """
    prov = provenance if provenance is not None else current_provenance()
    values: dict[str, Any] = {
        "memory_id": memory_id,
        "context_id": context_id,
        "version": version,
        "kind": kind,
        "prior": prior or {},
        "writer": writer,
        "actor_user_id": actor_user_id,
        "agent_id": prov.agent_id,
        "session_id": prov.session_id,
    }
    if at is not None:
        values["at"] = at
    return values


def stamp_writer(memory: Any, provenance: WriteProvenance) -> None:
    """Record ``provenance`` as the memory's last writer (#2044).

    Assigns only what differs, so a row that already carries these values
    (set when it was built) is not made dirty again after its INSERT.
    """
    if getattr(memory, "agent_id", None) != provenance.agent_id:
        memory.agent_id = provenance.agent_id
    if getattr(memory, "session_id", None) != provenance.session_id:
        memory.session_id = provenance.session_id


def record_revision(
    db: AsyncSession,
    memory: Any,
    *,
    kind: str,
    actor_user_id: str,
    at: datetime | None = None,
    prior: dict[str, Any] | None = None,
    writer: str = REVISION_WRITER_API,
    provenance: WriteProvenance | None = None,
) -> None:
    """Add one revision row for ``memory`` at its current version.

    ``at`` is the timestamp the write stamped on the row (``updated_at`` /
    ``deleted_at``), so the log and the row agree; omitted, the column's
    ``now()`` default applies (the transaction time, which is what a new
    row's ``created_at`` holds). The row's ``agent_id`` / ``session_id`` are
    the writer's (#2044), which also become the memory's last writer.

    A legacy row without a context gets no revision: the column is NOT NULL
    and no context-scoped read can reach such a row.

    Args:
        db: The session that writes the event; the caller commits.
        memory: The row the event is about, at its post-event version.
        kind: One of ``REVISION_KINDS``.
        actor_user_id: The caller who wrote the event.
        at: The event time, or None for the database default.
        prior: ``changed_prior`` of an ``updated`` event; empty otherwise.
        writer: One of ``REVISION_WRITERS``.
        provenance: The writer's agent and session; None reads the current
            request's (``current_provenance``).
    """
    prov = provenance if provenance is not None else current_provenance()
    stamp_writer(memory, prov)
    if memory.context_id is None:
        return
    db.add(
        MemoryRevision(
            **revision_values(
                memory_id=memory.id,
                context_id=memory.context_id,
                version=memory.version,
                kind=kind,
                actor_user_id=actor_user_id,
                at=at,
                prior=prior,
                writer=writer,
                provenance=prov,
            )
        )
    )


def record_edit(
    db: AsyncSession,
    memory: Any,
    before: dict[str, Any],
    *,
    actor_user_id: str,
    writer: str = REVISION_WRITER_API,
    provenance: WriteProvenance | None = None,
) -> bool:
    """Bump the version and log the edit when it changed a versioned field.

    Args:
        db: The edit's session; the caller commits.
        memory: The locked row with the edit applied (``updated_at`` stamped).
        before: ``versioned_snapshot`` taken before the edit.
        actor_user_id: The caller who made the edit.
        writer: ``rollback`` when ``rollback_session`` restores (#2044).
        provenance: The writer's agent and session (default: the request's).

    Returns:
        True when the version moved.
    """
    prior = changed_prior(before, memory)
    if not prior:
        return False
    memory.version = memory.version + 1
    record_revision(
        db,
        memory,
        kind=REVISION_KIND_UPDATED,
        actor_user_id=actor_user_id,
        at=memory.updated_at,
        prior=prior,
        writer=writer,
        provenance=provenance,
    )
    return True
