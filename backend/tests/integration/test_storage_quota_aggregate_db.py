"""DB-level pin for storage-quota aggregation ghost-row exclusion (#995 / #962).

``storage_quota_service._aggregate_from_db`` is the cold-start source of truth
for a workspace's committed storage usage (reseeds the Redis counter). It MUST
count only live committed bytes — ``status='uploaded'`` AND not soft-deleted —
so failed/reserved/deleted ghost rows do not inflate usage against the tier cap
(the #962 coordination requirement: ghost rows must not count against quota, or
billing disputes follow).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from models.file_objects import FileObject
from services import storage_quota_service
from utils.datetime import utcnow


async def _seed_workspace(db_session):
    ws_id = uuid4()
    await db_session.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, 'basic')"
        ),
        {"id": ws_id, "name": f"ws-{ws_id.hex[:8]}", "owner": "owner-995"},
    )
    return ws_id


def _file(ws_id, *, size_bytes, status, deleted=False):
    sha = uuid4().hex + uuid4().hex  # 64 hex chars, unique (partial-unique index)
    return FileObject(
        id=uuid4(),
        workspace_id=ws_id,
        sha256=sha,
        size_bytes=size_bytes,
        content_type="application/octet-stream",
        filename="f.bin",
        storage_backend="r2",
        # valid_file_storage_shape: non-reserved rows require a storage_key.
        storage_key=f"{ws_id}/{sha[:2]}/{sha}",
        status=status,
        created_by="owner-995",
        # deleted_at is a naive-UTC column (TIMESTAMP WITHOUT TIME ZONE).
        deleted_at=utcnow() if deleted else None,
    )


@pytest.mark.asyncio
async def test_aggregate_counts_only_live_uploaded_bytes(db_session):
    ws_id = await _seed_workspace(db_session)

    live = _file(ws_id, size_bytes=100, status="uploaded")
    failed = _file(ws_id, size_bytes=999, status="failed")
    reserved = _file(ws_id, size_bytes=50, status="reserved")
    deleted = _file(ws_id, size_bytes=888, status="uploaded", deleted=True)
    db_session.add_all([live, failed, reserved, deleted])
    await db_session.commit()

    total = await storage_quota_service._aggregate_from_db(ws_id, db_session)

    # Only the single live uploaded row (100) counts — failed (999), reserved
    # (50, tracked in Redis), and soft-deleted (888) ghost rows are excluded.
    assert total == 100


@pytest.mark.asyncio
async def test_aggregate_zero_for_workspace_with_only_ghost_rows(db_session):
    ws_id = await _seed_workspace(db_session)
    db_session.add_all(
        [
            _file(ws_id, size_bytes=500, status="failed"),
            _file(ws_id, size_bytes=500, status="reserved"),
        ]
    )
    await db_session.commit()

    total = await storage_quota_service._aggregate_from_db(ws_id, db_session)
    assert total == 0
