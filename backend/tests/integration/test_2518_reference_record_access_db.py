"""``record_access`` on ``POST /api/v1/memory/reference`` (#2518), on Postgres.

A REST read records the memory's access stats (``access_count``, the #1046
adoption signal ``reference_count``, ``last_used_at``) as the MCP tool does.
``record_access: false`` reads the memory without them: the web dialog's
re-read after a refused accept is not an adoption read. The access check and
the response are the same either way.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from api.routes.memory import reference
from models.memory import Memory
from models.schemas import ReferenceRequest
from services.memory_service import MemoryService
from tests.integration.test_identity_link_writes_db import _seed
from tests.integration.test_identity_links_db import _memory


async def _stats(db, memory_id) -> tuple[int, int, object]:
    db.expire_all()
    row = (
        await db.execute(
            select(Memory.access_count, Memory.reference_count, Memory.last_used_at).where(
                Memory.id == memory_id
            )
        )
    ).one()
    return row.access_count, row.reference_count, row.last_used_at


async def _read(db, user_id: str, memory_id, **body):
    return await reference(
        ReferenceRequest(memory_id=memory_id, **body),
        {"user_id": user_id},
        MemoryService(db),
    )


@pytest.mark.asyncio
async def test_record_access_false_leaves_the_access_stats_alone(db_session):
    a, _, _, ctx, _ = await _seed(db_session, private=False, link=False)
    memory_id, user_id = (await _memory(db_session, ctx, a)).id, a.user_id
    await db_session.commit()
    before = await _stats(db_session, memory_id)

    response = await _read(db_session, user_id, memory_id, record_access=False)

    assert response.memory_id == memory_id
    assert await _stats(db_session, memory_id) == before


@pytest.mark.asyncio
async def test_a_reference_records_the_read_by_default(db_session):
    a, _, _, ctx, _ = await _seed(db_session, private=False, link=False)
    memory_id, user_id = (await _memory(db_session, ctx, a)).id, a.user_id
    await db_session.commit()
    access_before, adoption_before, _ = await _stats(db_session, memory_id)

    await _read(db_session, user_id, memory_id)

    access, adoption, last_used_at = await _stats(db_session, memory_id)
    assert (access, adoption) == (access_before + 1, adoption_before + 1)
    assert last_used_at is not None


def test_the_request_defaults_to_recording():
    from uuid import uuid4

    assert ReferenceRequest(memory_id=uuid4()).record_access is True
