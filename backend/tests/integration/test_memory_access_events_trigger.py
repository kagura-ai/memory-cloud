"""Integration tests for the memory_access_events append-only trigger (#1278).

Against a real Postgres test DB. ``create_all`` does not install triggers, so
(like ``test_secret_store_integration``) we install the migration's DDL here,
then assert: DELETE and TRUNCATE always raise; an UPDATE touching a
non-carve-out column raises; an UPDATE limited to the carve-out
(user_id, session_id, run_id, event_metadata) is permitted (the erasure lane).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory_access_event import MemoryAccessEvent

_TRIGGER_DDL = """
CREATE OR REPLACE FUNCTION memory_access_events_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(OLD) - ARRAY['user_id','session_id','run_id','event_metadata'])
           IS DISTINCT FROM
           (to_jsonb(NEW) - ARRAY['user_id','session_id','run_id','event_metadata']) THEN
            RAISE EXCEPTION 'memory_access_events is append-only'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'memory_access_events is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


@pytest_asyncio.fixture
async def mae_triggers(db_session: AsyncSession):
    # Defensive: a prior test whose body raised inside the trigger leaves the
    # committed triggers in place (db_session commits real transactions, no
    # per-test rollback of DDL). Drop-if-exists first so setup is idempotent.
    await db_session.rollback()
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS memory_access_events_no_truncate ON memory_access_events")
    )
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS memory_access_events_no_mutate ON memory_access_events")
    )
    await db_session.execute(text(_TRIGGER_DDL))
    await db_session.execute(
        text(
            "CREATE TRIGGER memory_access_events_no_mutate "
            "BEFORE UPDATE OR DELETE ON memory_access_events "
            "FOR EACH ROW EXECUTE FUNCTION memory_access_events_append_only()"
        )
    )
    await db_session.execute(
        text(
            "CREATE TRIGGER memory_access_events_no_truncate "
            "BEFORE TRUNCATE ON memory_access_events "
            "FOR EACH STATEMENT EXECUTE FUNCTION memory_access_events_append_only()"
        )
    )
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS memory_access_events_no_truncate ON memory_access_events")
    )
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS memory_access_events_no_mutate ON memory_access_events")
    )
    await db_session.execute(text("DELETE FROM memory_access_events"))
    await db_session.execute(text("DROP FUNCTION IF EXISTS memory_access_events_append_only()"))
    await db_session.commit()


async def _insert(db: AsyncSession) -> int:
    row = MemoryAccessEvent(
        workspace_id=uuid.uuid4(),
        user_id="subject-1",
        principal_type="api_key",
        agent_id=uuid.uuid4(),
        session_id="sess-1",
        run_id="run-1",
        surface="mcp",
        operation="recall",
        outcome="success",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


@pytest.mark.asyncio
async def test_delete_blocked(db_session: AsyncSession, mae_triggers):
    rid = await _insert(db_session)
    with pytest.raises(Exception, match="append-only"):
        await db_session.execute(text("DELETE FROM memory_access_events WHERE id = :i"), {"i": rid})
    await db_session.rollback()


@pytest.mark.asyncio
async def test_truncate_blocked(db_session: AsyncSession, mae_triggers):
    await _insert(db_session)
    with pytest.raises(Exception, match="append-only"):
        await db_session.execute(text("TRUNCATE memory_access_events"))
    await db_session.rollback()


@pytest.mark.asyncio
async def test_immutable_column_update_blocked(db_session: AsyncSession, mae_triggers):
    rid = await _insert(db_session)
    with pytest.raises(Exception, match="append-only"):
        await db_session.execute(
            text("UPDATE memory_access_events SET operation = 'forget' WHERE id = :i"),
            {"i": rid},
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_carve_out_update_permitted(db_session: AsyncSession, mae_triggers):
    rid = await _insert(db_session)
    await db_session.execute(
        text(
            "UPDATE memory_access_events SET user_id = 'pseudo', session_id = NULL, "
            'run_id = NULL, event_metadata = \'{"redacted":"erased_subject"}\'::jsonb '
            "WHERE id = :i"
        ),
        {"i": rid},
    )
    await db_session.commit()
    row = await db_session.get(MemoryAccessEvent, rid)
    assert row.user_id == "pseudo"
    assert row.session_id is None
