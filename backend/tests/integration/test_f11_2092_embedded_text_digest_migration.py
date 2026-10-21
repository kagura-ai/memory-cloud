"""DB contract for f11_2092_embedded_text_digest (#2092).

The backfill's SQL ``md5(summary || chr(31) || content)`` must equal the
pipeline's Python ``embedded_text_digest`` byte for byte (Japanese text
included), or the first re-queue after the deploy would detect again.
"""

import uuid

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
