"""Real Alembic upgrade/backfill/retry/downgrade for durable erasure policy."""

from uuid import uuid4

from sqlalchemy import text

from alembic import command
from models.memory import Memory
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _downgrade_to,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

PRIOR = "f18_2395_file_cleanup"
REVISION = "f19_2391_embedding_erasure"
MARKER = "erased_author: the author's account was erased; its vector points are deleted by design (admin retry-failed re-embeds under the pseudonym)"


def test_f19_backfill_is_exact_batched_and_retryable():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR)
        erased_id, normal_id, workspace_id = uuid4(), uuid4(), uuid4()
        with engine.begin() as conn:
            for memory_id, error in [(erased_id, MARKER), (normal_id, MARKER + " extra")]:
                conn.execute(
                    Memory.__table__.insert().values(
                        id=memory_id,
                        workspace_id=workspace_id,
                        user_id="migration-author",
                        summary="note",
                        content="content",
                        type="note",
                        scope="working",
                        client="test",
                        embedding_status="failed",
                        embedding_error=error,
                        embedding_retry_count=3,
                    )
                )
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            rows = dict(
                conn.execute(
                    text(
                        "SELECT id, embedding_suppressed_at FROM memories WHERE workspace_id=:workspace"
                    ),
                    {"workspace": workspace_id},
                ).all()
            )
            assert rows[erased_id] is not None
            assert rows[normal_id] is None
            columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_name='vector_erasure_jobs'"
                    )
                ).scalars()
            )
            assert columns == {
                "id",
                "request_id",
                "memory_id",
                "point_id",
                "subject_pseudonym",
                "generation",
                "created_at",
                "completed_at",
                "captured_before_erasure",
            }
        # Simulate failure after autocommit batches but before revision stamp.
        with _alembic_at_test_db():
            command.stamp(_get_alembic_config(), PRIOR)
        _upgrade_to(REVISION)
        with engine.begin() as conn:
            # An older blue-green process can still omit both new columns.
            conn.execute(
                Memory.__table__.insert().values(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    user_id="old-process",
                    summary="note",
                    content="content",
                    type="note",
                    scope="working",
                    client="test",
                )
            )
        _downgrade_to(PRIOR)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('public.vector_erasure_jobs')")).scalar()
                is None
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_name='memories' AND column_name='embedding_suppressed_at'"
                    )
                ).scalar()
                == 0
            )
    finally:
        engine.dispose()
