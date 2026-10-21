"""#2043: memory_revisions CHECK + classification pins.

The CHECK constraints are derived from ordered tuples so
``Base.metadata.create_all()`` produces literals byte-identical to the alembic
migration (``f03_2043_memory_versions``); adding a value requires the
coordinated edits caught here (the ``test_memory_access_event_constants``
pattern). #2044 writes ``restored`` and ``rollback`` rows, so both are in the
vocabulary from the start.
"""

from models.memory import (
    REVISION_KINDS,
    REVISION_WRITERS,
    Memory,
    MemoryRevision,
)


def _check(name: str) -> str:
    c = next(c for c in MemoryRevision.__table_args__ if getattr(c, "name", None) == name)
    return c.sqltext.text


def test_kind_and_writer_vocabularies() -> None:
    assert REVISION_KINDS == ("created", "updated", "forgotten", "restored")
    assert REVISION_WRITERS == ("api", "rollback")


def test_check_literals_byte_identical_to_migration() -> None:
    assert _check("valid_revision_kind") == (
        "kind IN ('created', 'updated', 'forgotten', 'restored')"
    )
    assert _check("valid_revision_writer") == "writer IN ('api', 'rollback')"


def test_revisions_go_with_their_memory() -> None:
    (fk,) = MemoryRevision.__table__.foreign_keys
    assert fk.column.table.name == "memories"
    assert fk.ondelete == "CASCADE"
    assert fk.constraint is not None and fk.constraint.name == "fk_memory_revisions_memory_id"


def test_memory_version_starts_at_one() -> None:
    column = Memory.__table__.c.version
    assert column.nullable is False
    assert column.server_default is not None and column.server_default.arg == "1"


def test_classified_raw_exportable() -> None:
    from models.data_boundary import RAW_EXPORTABLE_TABLES

    assert "memory_revisions" in RAW_EXPORTABLE_TABLES


def test_revision_item_schema_follows_the_vocabularies() -> None:
    """#2043 PR2: the REST item spells the kinds / writers as Literals; a value
    appended to the tuples must reach the response schema too."""
    from typing import get_args

    from models.schemas import MemoryRevisionItem

    fields = MemoryRevisionItem.model_fields
    assert get_args(fields["kind"].annotation) == REVISION_KINDS
    assert get_args(fields["writer"].annotation) == REVISION_WRITERS


def test_actor_index_serves_erasure() -> None:
    """#2115: account erasure finds the subject's events by actor alone. Full,
    not partial (the column is NOT NULL), and declared plainly — the migration
    builds it CONCURRENTLY, ``create_all`` cannot."""
    (index,) = [
        i for i in MemoryRevision.__table__.indexes if i.name == "idx_memory_revisions_actor"
    ]
    assert [c.name for c in index.columns] == ["actor_user_id"]
    assert MemoryRevision.__table__.c.actor_user_id.nullable is False
    assert index.dialect_options["postgresql"]["where"] is None
    assert not index.dialect_options["postgresql"]["concurrently"]
