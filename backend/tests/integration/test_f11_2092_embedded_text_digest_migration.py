"""DB contract for f11_2092_embedded_text_digest (#2092).

The backfill's SQL ``md5(summary || chr(31) || content)`` must equal the
pipeline's Python ``embedded_text_digest`` byte for byte (Japanese text
included), or the first re-queue after the deploy would detect again.

On a FIPS-mode server ``md5()`` raises (#2511): the backfill is skipped and the
upgrade completes. That server is simulated by a ``md5(text)`` in a schema
searched before ``pg_catalog``, set on the test database for the upgrade only.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from services.supersede_verify import embedded_text_digest
from tests.integration.test_alembic_migrations import (
    TestB03NeuralEdgesBackfillMigration,
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_and_upgrade_to,
    _sync_engine,
)

PRE_F11_REV = "f10_2115_revisions_actor_idx"
F11_REV = "f11_2092_embedded_text_digest"


def _seed_memory(
    conn: Connection,
    owner: str,
    ws_id: str,
    ctx_id: str,
    *,
    summary: str,
    content: str,
    status: str,
    deleted: bool = False,
) -> str:
    mem_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO memories "
            "(id, user_id, workspace_id, context_id, summary, content, type, "
            " embedding_status, importance, confidence, scope, long_term, "
            " access_count, client, source, deleted_at) "
            "VALUES (:id, :u, :ws, :ctx, :summary, :content, 'note', :status, "
            " 0.5, 1.0, 'working', false, 0, 'test', 'test', :deleted_at)"
        ),
        {
            "id": mem_id,
            "u": owner,
            "ws": ws_id,
            "ctx": ctx_id,
            "summary": summary,
            "content": content,
            "status": status,
            "deleted_at": "2026-01-01T00:00:00" if deleted else None,
        },
    )
    return mem_id


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


# What a FIPS-mode server raises for md5() (src/backend/utils/adt/
# cryptohashfuncs.c): PG 15+ XX000 "could not compute MD5 hash: <OpenSSL
# reason>", PG 14 53200 "out of memory". The last two must still abort the
# upgrade: an md5() refused for another reason, and an md5() that works on the
# probe but fails inside the UPDATE.
_SIMULATED_MD5_BODIES = {
    "fips_pg15": (
        "BEGIN RAISE EXCEPTION USING ERRCODE = 'internal_error', "
        "MESSAGE = 'could not compute MD5 hash: unsupported'; END"
    ),
    "fips_pg14": (
        "BEGIN RAISE EXCEPTION USING ERRCODE = 'out_of_memory', MESSAGE = 'out of memory'; END"
    ),
    "other_error": (
        "BEGIN RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege', "
        "MESSAGE = 'permission denied for function md5'; END"
    ),
    "fails_in_update": (
        "BEGIN IF value = '' THEN RETURN pg_catalog.md5(value); END IF; "
        "RAISE EXCEPTION USING ERRCODE = 'internal_error', "
        "MESSAGE = 'could not compute MD5 hash: simulated'; END"
    ),
}


def _simulate_md5(conn: Connection, case: str) -> None:
    """Shadow ``md5(text)`` for new connections to the test database."""
    conn.execute(text("CREATE SCHEMA fips_md5_sim"))
    conn.execute(
        text(
            "CREATE FUNCTION fips_md5_sim.md5(value text) RETURNS text "
            "LANGUAGE plpgsql AS $fn$ " + _SIMULATED_MD5_BODIES[case] + " $fn$"
        )
    )
    conn.execute(
        text(
            "DO $$ BEGIN EXECUTE format("
            "'ALTER DATABASE %I SET search_path = fips_md5_sim, pg_catalog, public', "
            "current_database()); END $$"
        )
    )


def _restore_md5() -> None:
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "DO $$ BEGIN EXECUTE format('ALTER DATABASE %I RESET search_path', "
                "current_database()); END $$"
            )
        )
        conn.execute(text("DROP SCHEMA IF EXISTS fips_md5_sim CASCADE"))
    engine.dispose()


def _column_exists(conn: Connection) -> bool:
    return (
        conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'memories' AND column_name = 'embedded_text_digest'"
            )
        ).scalar_one()
        == 1
    )


class TestF11EmbeddedTextDigestMigration:
    def test_success_rows_are_backfilled_with_the_python_digest(self) -> None:
        try:
            _reset_and_upgrade_to(PRE_F11_REV)
            owner = "owner-2092-digest"
            cases = [
                ("ascii summary", "ascii content", "success", False, True),
                ("日本語の要約", "本文:記憶の詳細です。", "success", False, True),
                ("pending summary", "pending content", "pending", False, False),
                ("failed summary", "failed content", "failed", False, False),
                ("deleted summary", "deleted content", "success", True, True),
            ]
            engine = _sync_engine()
            with engine.begin() as conn:
                ws_id, ctx_id = TestB03NeuralEdgesBackfillMigration._seed_workspace_and_context(
                    conn, owner
                )
                seeded = [
                    (
                        _seed_memory(
                            conn,
                            owner,
                            ws_id,
                            ctx_id,
                            summary=summary,
                            content=content,
                            status=status,
                            deleted=deleted,
                        ),
                        summary,
                        content,
                        stamped,
                    )
                    for summary, content, status, deleted, stamped in cases
                ]
            engine.dispose()

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F11_REV)

            engine = _sync_engine()
            with engine.begin() as conn:
                for mem_id, summary, content, stamped in seeded:
                    digest = conn.execute(
                        text("SELECT embedded_text_digest FROM memories WHERE id = :id"),
                        {"id": mem_id},
                    ).scalar_one()
                    expected = embedded_text_digest(summary, content) if stamped else None
                    assert digest == expected, f"{summary!r}: {digest!r} != {expected!r}"
            engine.dispose()
        finally:
            _leave_db_at_head()

    @pytest.mark.parametrize("case", ["fips_pg15", "fips_pg14"])
    def test_fips_md5_leaves_the_rows_null_and_the_upgrade_completes(self, case: str) -> None:
        """#2511: the guarded backfill skips an unavailable ``md5()``; the
        column is added and every row stays NULL (detected once on its next
        re-queue)."""
        try:
            _reset_and_upgrade_to(PRE_F11_REV)
            owner = "owner-2511-fips"
            engine = _sync_engine()
            with engine.begin() as conn:
                ws_id, ctx_id = TestB03NeuralEdgesBackfillMigration._seed_workspace_and_context(
                    conn, owner
                )
                mem_id = _seed_memory(
                    conn,
                    owner,
                    ws_id,
                    ctx_id,
                    summary="fips summary",
                    content="fips content",
                    status="success",
                )
                _simulate_md5(conn, case)
            engine.dispose()

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), F11_REV)
            _restore_md5()

            engine = _sync_engine()
            with engine.begin() as conn:
                assert _column_exists(conn)
                digest = conn.execute(
                    text("SELECT embedded_text_digest FROM memories WHERE id = :id"),
                    {"id": mem_id},
                ).scalar_one()
                assert digest is None
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                assert version == F11_REV
            engine.dispose()
        finally:
            _restore_md5()
            _leave_db_at_head()

    @pytest.mark.parametrize("case", ["other_error", "fails_in_update"])
    def test_any_other_md5_failure_still_aborts_the_upgrade(self, case: str) -> None:
        """Only an ``md5()`` that FIPS mode refuses outright is skipped: any
        other error, and an error of the UPDATE itself, rolls the upgrade
        back."""
        try:
            _reset_and_upgrade_to(PRE_F11_REV)
            owner = "owner-2511-abort"
            engine = _sync_engine()
            with engine.begin() as conn:
                ws_id, ctx_id = TestB03NeuralEdgesBackfillMigration._seed_workspace_and_context(
                    conn, owner
                )
                _seed_memory(
                    conn,
                    owner,
                    ws_id,
                    ctx_id,
                    summary="abort summary",
                    content="abort content",
                    status="success",
                )
                _simulate_md5(conn, case)
            engine.dispose()

            with _alembic_at_test_db(), pytest.raises(Exception, match="md5|MD5"):
                command.upgrade(_get_alembic_config(), F11_REV)
            _restore_md5()

            engine = _sync_engine()
            with engine.begin() as conn:
                assert not _column_exists(conn)
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                assert version == PRE_F11_REV
            engine.dispose()
        finally:
            _restore_md5()
            _leave_db_at_head()

    def test_downgrade_drops_the_column(self) -> None:
        try:
            _reset_and_upgrade_to(F11_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                assert _column_exists(conn)
            engine.dispose()

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_F11_REV)

            engine = _sync_engine()
            with engine.begin() as conn:
                assert not _column_exists(conn)
            engine.dispose()
        finally:
            _leave_db_at_head()
