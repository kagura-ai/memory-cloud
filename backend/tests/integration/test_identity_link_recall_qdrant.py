"""#1807: recall with linked accounts against a live Postgres and Qdrant.

The unit suite pins the vector filter's shape (``MatchAny`` over the link
set); this drives ``SearchService`` end to end — the link set read from
Postgres, the filter applied by a real Qdrant — so the two halves are proved
to agree. Only the embedding call and the search config are stubbed.

Skipped when ``QDRANT_URL`` is unreachable (and by ``db_session`` when
Postgres is). Writes to a throwaway ``kagura_memories_it_*`` collection.
Run with ``make test-integration``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient

from db import qdrant as qdrant_module
from db.qdrant import add_memory_to_qdrant, ensure_kagura_memories_collection
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from services.identity_link_service import IdentityLinkService
from services.search_service import SearchService

_DIM = 8


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


@pytest_asyncio.fixture(loop_scope="session")
async def collection(monkeypatch) -> AsyncIterator[str]:
    client = AsyncQdrantClient(url=_qdrant_url(), timeout=5)
    try:
        await client.get_collections()
    except Exception as exc:  # noqa: BLE001 — intentional broad skip guard
        await client.close()
        pytest.skip(f"Qdrant unreachable at {_qdrant_url()}: {exc}")
    monkeypatch.setattr(qdrant_module, "_qdrant_client", client)
    name = f"kagura_memories_it_{uuid4().hex[:12]}"
    await ensure_kagura_memories_collection(_DIM, name)
    yield name
    await client.delete_collection(name)
    await client.close()


async def _user(db, prefix: str) -> User:
    uid = f"{prefix}_{uuid4().hex[:10]}"
    user = User(
        user_id=uid,
        email=f"{uid}@recall.example",
        name=uid,
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
    )
    db.add(user)
    await db.flush()
    return user


async def _point(user: User, context: Context, collection: str) -> UUID:
    memory_id = uuid4()
    await add_memory_to_qdrant(
        user_id=user.user_id,
        memory_id=memory_id,
        vector=[0.1] * _DIM,
        payload={"summary": f"written by {user.user_id}"},
        workspace_id=str(context.workspace_id),
        context_id=str(context.id),
        collection_name=collection,
    )
    return memory_id


async def _recall(db, collection: str, user: User, context: Context) -> set[str]:
    embed = MagicMock()
    embed.embed_with_usage = AsyncMock(return_value=([0.1] * _DIM, 0))
    service = SearchService(db)
    service._get_search_config = AsyncMock(
        return_value=SimpleNamespace(
            semantic_weight=1.0, bm25_weight=0.0, fetch_factor=2, use_rerank=False
        )
    )
    with patch(
        "services.search_service.resolve_routing_from_config", return_value=(collection, embed)
    ):
        results = await service.hybrid_search(
            query="what was written",
            user_id=user.user_id,
            workspace_id=str(context.workspace_id),
            context_id=str(context.id),
            k=10,
            use_rerank=False,
            search_mode="semantic",
        )
    return {str(r["id"]) for r in results}


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(db_session, collection):
    admin, oauth, stranger = (
        await _user(db_session, "local"),
        await _user(db_session, "google"),
        await _user(db_session, "other"),
    )
    workspace = Workspace(id=uuid4(), name="Recall Link Test", owner_user_id=admin.user_id)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=admin.user_id, role=WorkspaceRole.OWNER)
    )
    for member in (oauth, stranger):
        db_session.add(
            WorkspaceMember(
                workspace_id=workspace.id, user_id=member.user_id, role=WorkspaceRole.ADMIN
            )
        )
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"priv_{uuid4().hex[:8]}",
        display_name="Private",
        created_by=admin.user_id,
        is_private=True,
    )
    db_session.add(context)
    await db_session.flush()
    points = {
        "admin": await _point(admin, context, collection),
        "oauth": await _point(oauth, context, collection),
        # A row of another member, e.g. from before the context went private.
        "stranger": await _point(stranger, context, collection),
    }
    return SimpleNamespace(
        admin=admin, oauth=oauth, stranger=stranger, context=context, points=points
    )


class TestRecallAcrossLinkedAccounts:
    @pytest.mark.asyncio
    async def test_unlinked_accounts_recall_only_their_own(self, db_session, collection, seeded):
        found = await _recall(db_session, collection, seeded.oauth, seeded.context)

        assert found == {str(seeded.points["oauth"])}

    @pytest.mark.asyncio
    async def test_a_linked_account_recalls_both_authors_and_no_one_else(
        self, db_session, collection, seeded
    ):
        await IdentityLinkService(db_session).link(seeded.admin.user_id, seeded.oauth.user_id)

        for caller in (seeded.oauth, seeded.admin):
            found = await _recall(db_session, collection, caller, seeded.context)
            assert found == {str(seeded.points["admin"]), str(seeded.points["oauth"])}

    @pytest.mark.asyncio
    async def test_unlink_narrows_recall_again_at_once(self, db_session, collection, seeded):
        service = IdentityLinkService(db_session)
        await service.link(seeded.admin.user_id, seeded.oauth.user_id)
        await service.unlink(seeded.oauth.user_id, seeded.admin.user_id)

        found = await _recall(db_session, collection, seeded.oauth, seeded.context)

        assert found == {str(seeded.points["oauth"])}
