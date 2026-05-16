"""Data-bearing round-trip + audit assertion tests for migration a97.

Issue #323: the schema-only round-trip in ``test_alembic_migrations.py``
runs against an empty database, so it cannot catch backfill bugs. These
tests seed a realistic single-workspace dataset before crossing the a97
boundary and verify:

    - ``resources`` is populated, ``resource_pk`` is fully backfilled on
      all four satellite tables, ``resource_tokens.workspace_id`` is
      backfilled, and the partial UNIQUE allows upsert → delete → upsert
      revival (the business invariant behind the ``op='upsert'``
      predicate).
    - ``downgrade`` restores the a96 schema and the original data is
      still readable through the legacy ``resource_id`` slug.
    - Re-running ``upgrade`` after a full round-trip is idempotent.
    - The pre-migration audit aborts when a satellite table references a
      ``resource_id`` that has no matching active context, with an
      actionable error message.
"""

import contextlib
import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from alembic import command

from .test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
)


def _leave_db_at_head() -> None:
    """Restore the test DB to alembic head after a partial-revision test.

    These tests intentionally downgrade to ``a96`` mid-test and re-upgrade
    to ``a97`` for the round-trip assertion. Without this restore, the DB
    is left at ``a97`` (NOT at head), which breaks any subsequent
    integration test whose ORM model has columns from migrations beyond
    ``a97`` — e.g. ``test_role_manager_email_sync`` fails when it tries
    to insert a ``users`` row that references ``workspace_slot_bonus``
    (added in ``e15_675``) on the still-at-``a97`` schema.

    The audit-failure test leaves the DB mid-migration (alembic's
    transactional DDL rolls the partial migration back, but the prior
    state may be unusual). Reset to a clean baseline first via
    ``_reset_alembic_state()`` before upgrading, so this helper is
    safe to call from any test's finally block regardless of how the
    test exited.
    """
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


_A96 = "a96_ctx_resource_id_unique"
_A97 = "a97_resources_entity"

_DEFAULT_TEST_URL = "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test"


def _test_url() -> str:
    return os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL)


def _sync_engine() -> sa.Engine:
    """Build a synchronous engine for raw seed/assert SQL (Alembic sync).

    Normalizes whichever async driver the env specifies down to plain
    ``postgresql`` so psycopg2 handles the test connection regardless of
    whether TEST_DATABASE_URL names asyncpg, aiopg, or no driver at all.
    """
    url = make_url(_test_url()).set(drivername="postgresql")
    return create_engine(url)


@contextlib.contextmanager
def _point_alembic_at_test_db() -> Iterator[None]:
    """Force alembic's env.py to target TEST_DATABASE_URL during the block.

    ``env.py`` reads ``DATABASE_URL`` via ``get_database_url()`` and ignores
    the URL set on the Alembic ``Config`` object. Without this shim,
    ``command.upgrade`` would run against the dev database while
    ``_reset_alembic_state`` wipes the isolated test database — exactly
    the silent pass-through that lets the existing round-trip tests
    succeed without actually exercising the test DB.
    """
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _test_url()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _seed_workspace_and_context(
    conn: sa.Connection, *, workspace_id: uuid.UUID, resource_id: str, context_id: uuid.UUID
) -> None:
    """Minimal workspace + public context needed to back a resource."""
    owner_id = "user-test"
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, 'free')"
        ),
        {
            "id": workspace_id,
            "name": f"test-ws-{workspace_id.hex[:8]}",
            "owner": owner_id,
        },
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by, "
            "is_private, is_public, resource_id) "
            "VALUES (:id, :ws, :name, :owner, false, true, :rid)"
        ),
        {
            "id": context_id,
            "ws": workspace_id,
            "name": f"ctx-{resource_id}",
            "owner": owner_id,
            "rid": resource_id,
        },
    )


class TestA97ResourcesMigration:
    """Data-bearing round-trip + audit assertion tests for a97."""

    def test_a97_data_bearing_roundtrip(self) -> None:
        """Full seed → upgrade → assert backfill → downgrade → upgrade cycle."""
        with _point_alembic_at_test_db():
            self._run_data_bearing_roundtrip()

    def _run_data_bearing_roundtrip(self) -> None:
        _reset_alembic_state()
        config = _get_alembic_config()

        # Stop at a96 so we can seed with the pre-a97 schema shape.
        command.upgrade(config, _A96)

        workspace_id = uuid.uuid4()
        context_id = uuid.uuid4()
        resource_id = "test-resource"

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                _seed_workspace_and_context(
                    conn,
                    workspace_id=workspace_id,
                    resource_id=resource_id,
                    context_id=context_id,
                )
                # upsert v1 → delete v1 → upsert v2 (event-sourcing revival).
                # The partial UNIQUE treats each version as a distinct upsert,
                # so revival must bump the version; two raw upserts of the same
                # (resource_pk, doc_id, version) would correctly collide.
                conn.execute(
                    text(
                        "INSERT INTO resource_events "
                        "(resource_id, op, doc_id, version, importance) VALUES "
                        "(:r, 'upsert', 'doc1', 1, 0.5), "
                        "(:r, 'delete', 'doc1', 1, 0.5), "
                        "(:r, 'upsert', 'doc1', 2, 0.5)"
                    ),
                    {"r": resource_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO resource_schemas "
                        "(resource_id, schema_version, field_definitions) "
                        "VALUES (:r, 1, '[]'::jsonb)"
                    ),
                    {"r": resource_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO indexer_state "
                        "(resource_id, context_id, last_offset, active_version, job_status) "
                        "VALUES (:r, :ctx, 0, 1, 'idle')"
                    ),
                    {"r": resource_id, "ctx": context_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO resource_tokens "
                        "(resource_id, token_hash, is_active, quota_events_per_hour) "
                        "VALUES (:r, 'hash-test', true, 1000)"
                    ),
                    {"r": resource_id},
                )

            # Cross the a97 boundary.
            command.upgrade(config, _A97)

            with engine.begin() as conn:
                # resources row was seeded from contexts.
                resources_row = conn.execute(
                    text(
                        "SELECT id, workspace_id, resource_id FROM resources WHERE resource_id = :r"
                    ),
                    {"r": resource_id},
                ).one()
                assert resources_row.workspace_id == workspace_id
                resource_pk = resources_row.id

                # resource_pk is fully backfilled on every satellite table.
                for table in (
                    "resource_events",
                    "resource_schemas",
                    "indexer_state",
                    "resource_tokens",
                ):
                    null_rows = conn.execute(
                        text(f"SELECT COUNT(*) FROM {table} WHERE resource_pk IS NULL")  # noqa: S608
                    ).scalar_one()
                    assert null_rows == 0, f"{table} has NULL resource_pk after a97"

                    nonmatching = conn.execute(
                        text(  # noqa: S608
                            f"SELECT COUNT(*) FROM {table} WHERE resource_pk != :pk"
                        ),
                        {"pk": resource_pk},
                    ).scalar_one()
                    assert nonmatching == 0, (
                        f"{table} has resource_pk rows pointing outside the seeded resource"
                    )

                # resource_tokens.workspace_id is NOT NULL and correct.
                ws_rows = conn.execute(
                    text("SELECT workspace_id FROM resource_tokens WHERE resource_id = :r"),
                    {"r": resource_id},
                ).all()
                assert ws_rows and all(row.workspace_id == workspace_id for row in ws_rows)

                # The partial UNIQUE index actually exists (guards against
                # a silent CONCURRENTLY failure that would leave the
                # revival-allowing invariant unprotected).
                index_exists = conn.execute(
                    text(
                        "SELECT 1 FROM pg_class c "
                        "JOIN pg_index i ON i.indexrelid = c.oid "
                        "WHERE c.relname = 'ux_resource_events_upsert_version' "
                        "AND i.indisunique AND i.indisvalid"
                    )
                ).scalar_one_or_none()
                assert index_exists is not None, (
                    "partial UNIQUE ux_resource_events_upsert_version must exist "
                    "and be VALID after a97 upgrade"
                )

                # The partial UNIQUE permits the revival pattern.
                event_count = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM resource_events "
                        "WHERE resource_pk = :pk AND doc_id = 'doc1'"
                    ),
                    {"pk": resource_pk},
                ).scalar_one()
                assert event_count == 3, "upsert → delete → upsert revival rows were lost"

                upsert_count = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM resource_events "
                        "WHERE resource_pk = :pk AND doc_id = 'doc1' AND op = 'upsert'"
                    ),
                    {"pk": resource_pk},
                ).scalar_one()
                assert upsert_count == 2, (
                    "both upsert rows must survive; the partial UNIQUE should only "
                    "reject a second concurrent upsert without an intervening delete"
                )

            # Snapshot the resources count so the idempotency check
            # below can compare against something non-trivial.
            with engine.begin() as conn:
                resources_count_pre = conn.execute(
                    text("SELECT COUNT(*) FROM resources")
                ).scalar_one()

            # Downgrade and confirm legacy reads still work.
            command.downgrade(config, _A96)
            with engine.begin() as conn:
                exists = conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = 'resources')"
                    )
                ).scalar_one()
                assert not exists, "resources table should be dropped on downgrade"

                legacy_count = conn.execute(
                    text("SELECT COUNT(*) FROM resource_events WHERE resource_id = :r"),
                    {"r": resource_id},
                ).scalar_one()
                assert legacy_count == 3, "legacy resource_id data must survive downgrade"

            # Re-upgrade to confirm idempotency under a full round-trip:
            # neither a row was lost across down/up, nor a duplicate
            # slipped past ``uq_resources_workspace_resource_id``.
            command.upgrade(config, _A97)
            with engine.begin() as conn:
                resources_count_post = conn.execute(
                    text("SELECT COUNT(*) FROM resources")
                ).scalar_one()
                assert resources_count_post == resources_count_pre, (
                    f"re-upgrade must preserve row count: "
                    f"pre={resources_count_pre} post={resources_count_post}"
                )
        finally:
            engine.dispose()
            _leave_db_at_head()

    def test_a97_audit_fails_on_orphan(self) -> None:
        """Satellite rows with no matching context must abort the migration.

        Seeds the orphan into ``resource_events`` as a representative case;
        the migration's audit loop covers all four satellite tables
        identically, so extending the fixture to the other three would
        only exercise the same code path.
        """
        with _point_alembic_at_test_db():
            self._run_audit_fails_on_orphan()

    def _run_audit_fails_on_orphan(self) -> None:
        _reset_alembic_state()
        config = _get_alembic_config()

        command.upgrade(config, _A96)

        workspace_id = uuid.uuid4()
        context_id = uuid.uuid4()
        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                # Real context for one resource so the table isn't empty,
                # plus an orphan event referencing a resource_id that
                # never had a public context.
                _seed_workspace_and_context(
                    conn,
                    workspace_id=workspace_id,
                    resource_id="real-resource",
                    context_id=context_id,
                )
                conn.execute(
                    text(
                        "INSERT INTO resource_events "
                        "(resource_id, op, doc_id, version, importance) "
                        "VALUES ('orphan-resource', 'upsert', 'doc1', 1, 0.5)"
                    )
                )

            with pytest.raises(RuntimeError, match="satellite rows reference"):
                command.upgrade(config, _A97)
        finally:
            engine.dispose()
            _leave_db_at_head()

    def test_a97_legacy_writes_without_resource_pk_still_work(self) -> None:
        """Phase 1 invariant: pre-a97 writer paths that only set
        ``resource_id`` must keep working after the migration. If this
        test ever fails, the migration has tightened a shadow column to
        NOT NULL prematurely and the next production deploy will
        throw ``NullViolationError`` on the first ingest request.
        """
        with _point_alembic_at_test_db():
            self._run_legacy_writes_without_resource_pk_still_work()

    def _run_legacy_writes_without_resource_pk_still_work(self) -> None:
        _reset_alembic_state()
        config = _get_alembic_config()
        command.upgrade(config, _A97)

        workspace_id = uuid.uuid4()
        context_id = uuid.uuid4()
        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                _seed_workspace_and_context(
                    conn,
                    workspace_id=workspace_id,
                    resource_id="legacy-writer",
                    context_id=context_id,
                )
                # Simulate a pre-#324 writer that only knows about
                # ``resource_id`` — no resource_pk, no workspace_id on
                # resource_tokens. Must succeed.
                # Two identical upserts with resource_pk = NULL: the
                # partial UNIQUE is ``WHERE resource_pk IS NOT NULL`` so
                # NULL rows are excluded from the uniqueness check and
                # the second INSERT must succeed. If this ever raises,
                # the partial predicate has regressed and Phase 1
                # writers would start throwing UniqueViolationError.
                conn.execute(
                    text(
                        "INSERT INTO resource_events "
                        "(resource_id, op, doc_id, version, importance) VALUES "
                        "('legacy-writer', 'upsert', 'doc-legacy', 1, 0.5), "
                        "('legacy-writer', 'upsert', 'doc-legacy', 1, 0.5)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO resource_schemas "
                        "(resource_id, schema_version, field_definitions) "
                        "VALUES ('legacy-writer', 1, '[]'::jsonb)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO indexer_state "
                        "(resource_id, context_id, last_offset, active_version, job_status) "
                        "VALUES ('legacy-writer', :ctx, 0, 1, 'idle')"
                    ),
                    {"ctx": context_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO resource_tokens "
                        "(resource_id, token_hash, is_active, quota_events_per_hour) "
                        "VALUES ('legacy-writer', 'hash-legacy', true, 1000)"
                    )
                )

            # All four tables accepted the NULL resource_pk.
            with engine.begin() as conn:
                for table in (
                    "resource_events",
                    "resource_schemas",
                    "indexer_state",
                    "resource_tokens",
                ):
                    null_rows = conn.execute(
                        text(f"SELECT COUNT(*) FROM {table} WHERE resource_pk IS NULL")  # noqa: S608
                    ).scalar_one()
                    assert null_rows >= 1, f"{table} should accept NULL resource_pk in Phase 1"

                # Both duplicate NULL upserts survived the partial UNIQUE.
                dup_null_upserts = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM resource_events "
                        "WHERE resource_pk IS NULL AND doc_id = 'doc-legacy' "
                        "AND version = 1 AND op = 'upsert'"
                    )
                ).scalar_one()
                assert dup_null_upserts == 2, (
                    "partial UNIQUE must exclude resource_pk=NULL rows so "
                    "Phase 1 legacy writers do not collide with themselves"
                )
        finally:
            engine.dispose()
            _leave_db_at_head()
