"""#2044: memory_session_rollbacks CHECK + schema pins.

The status CHECK is derived from the ordered tuple so ``create_all()`` and the
f04 migration carry the same literal (the ``test_memory_revision_constants``
pattern); the provenance columns, the partial index and the rollback target
key are pinned next to it.
"""

from models.memory import (
    SESSION_ROLLBACK_STATUSES,
    Memory,
    MemoryRevision,
    MemorySessionRollback,
)


def _check(name: str) -> str:
    c = next(c for c in MemorySessionRollback.__table_args__ if getattr(c, "name", None) == name)
    return c.sqltext.text


def test_status_vocabulary_and_literal() -> None:
    assert SESSION_ROLLBACK_STATUSES == ("in_progress", "completed", "partial")
    assert _check("valid_session_rollback_status") == (
        "status IN ('in_progress', 'completed', 'partial')"
    )


def test_provenance_columns_are_nullable_and_sized_like_the_audit_log() -> None:
    from models.memory_access_event import MemoryAccessEvent

    for column in (Memory.__table__.c.agent_id, Memory.__table__.c.session_id):
        assert column.nullable is True and column.server_default is None
    assert (
        Memory.__table__.c.session_id.type.length
        == MemoryRevision.__table__.c.session_id.type.length
        == MemoryAccessEvent.__table__.c.session_id.type.length
        == 128
    )
    assert MemoryRevision.__table__.c.undone_by.nullable is True


def test_context_session_index_is_partial() -> None:
    (index,) = [i for i in Memory.__table__.indexes if i.name == "idx_memories_context_session"]
    assert [c.name for c in index.columns] == ["context_id", "session_id"]
    assert str(index.dialect_options["postgresql"]["where"]) == "session_id IS NOT NULL"


def test_one_rollback_row_per_target() -> None:
    (index,) = [
        i
        for i in MemorySessionRollback.__table__.indexes
        if i.name == "uq_memory_session_rollbacks_target"
    ]
    assert index.unique is True
    rendered = [str(e) for e in index.expressions]
    assert rendered[:2] == [
        "memory_session_rollbacks.context_id",
        "memory_session_rollbacks.session_id",
    ]
    assert rendered[2] == "COALESCE(principal_user_id, '')"
    assert rendered[3].startswith("COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'")
    (fk,) = MemorySessionRollback.__table__.foreign_keys
    assert fk.column.table.name == "contexts" and fk.ondelete == "CASCADE"


def test_classified_operational() -> None:
    from models.data_boundary import OPERATIONAL_TABLES

    assert "memory_session_rollbacks" in OPERATIONAL_TABLES
