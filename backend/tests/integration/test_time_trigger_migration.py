import uuid

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_generated_trigger_columns_populate_from_details(db_session):
    """Inserting a time memory with details.trigger.from/until populates the
    generated trigger_from / trigger_until columns."""
    mem_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO memories
              (id, user_id, summary, content, type, importance, confidence,
               scope, embedding_status, client, source, long_term,
               access_count, details)
            VALUES
              (:id, 'u1', 'avoid', 'avoid', 'time', 0.5, 1.0,
               'working', 'success', 'test', 'mcp_remember', false, 0,
               CAST(:details AS JSONB))
            """
        ),
        {
            "id": mem_id,
            "details": '{"trigger": {"from": "2026-07-01T00:00:00", '
            '"until": "2026-07-31T23:59:59"}}',
        },
    )
    row = (
        await db_session.execute(
            text("SELECT trigger_from, trigger_until FROM memories WHERE id = :id"),
            {"id": mem_id},
        )
    ).one()
    # TEXT generated columns preserve the stored ISO string verbatim (the 'T'
    # separator), unlike a timestamp cast which would render with a space.
    assert row.trigger_from == "2026-07-01T00:00:00"
    assert row.trigger_until == "2026-07-31T23:59:59"
