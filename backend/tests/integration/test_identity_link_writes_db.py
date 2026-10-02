"""#1803: writes that name another memory, across linked accounts.

In a private context the owner is the link set (#1784): either account reads
all of it. These pin the writes that look a memory up by something other than
its id — an ``external_id`` upsert, and a remember's ``linked_memory_ids`` /
``linked_source_uris`` / ``supersedes`` — so they match the same set. A shared
context keeps matching the caller's own memories only.

Real-DB tests (``db_session`` skips without Postgres); they run in CI's
integration job with the rest of ``tests/integration/``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, User, Workspace
from models.memory import EDGE_TYPE_SUPERSEDES, Memory, NeuralMemoryEdge
from models.schemas import RememberRequest, UpdateMemoryRequest
from repositories.memory import MemoryRepository
from services import security_notification_service as sns
from services.identity_link_service import IdentityLinkService
from services.memory_service import MemoryService
from tests.integration.test_identity_links_db import (
    _memory,
    _private_context,
    _user,
    _workspace,
)

EXTERNAL_ID = "issue-1803"


async def _shared_context(db: AsyncSession, workspace: Workspace, creator: User) -> Context:
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"shared_{uuid4().hex[:8]}",
        display_name="Shared",
        created_by=creator.user_id,
        is_private=False,
    )
    db.add(context)
    await db.flush()
    return context


async def _external(db: AsyncSession, context: Context, author: User) -> Memory:
    memory = await _memory(db, context, author)
    memory.details = {"resource_id": EXTERNAL_ID}
    memory.source_uri = f"file://{uuid4().hex}"
    await db.flush()
    return memory


async def _seed(db: AsyncSession, *, private: bool = True, link: bool = True):
    """admin's context, written to by admin; oauth is in the workspace."""
    admin, oauth = await _user(db, "local"), await _user(db, "google")
    workspace = await _workspace(db, admin, oauth)
    make = _private_context if private else _shared_context
    context = await make(db, workspace, admin)
    by_admin = await _external(db, context, admin)
    if link:
        await IdentityLinkService(db).link(admin.user_id, oauth.user_id)
    return admin, oauth, workspace, context, by_admin


class TestResourceLookup:
    @pytest.mark.asyncio
    async def test_include_linked_finds_the_linked_accounts_row(self, db_session):
        _, oauth, _, context, by_admin = await _seed(db_session)
        repo = MemoryRepository(db_session)

        assert await repo.get_by_resource_id(EXTERNAL_ID, context.id, oauth.user_id) is None, (
            "the default stays the caller's own rows"
        )
        found = await repo.get_by_resource_id(
            EXTERNAL_ID, context.id, oauth.user_id, include_linked=True
        )
        assert found is not None and found.id == by_admin.id

    @pytest.mark.asyncio
    async def test_include_linked_never_reaches_a_stranger(self, db_session):
        _, oauth, _, context, _ = await _seed(db_session, link=False)

        assert (
            await MemoryRepository(db_session).get_by_resource_id(
                EXTERNAL_ID, context.id, oauth.user_id, include_linked=True
            )
            is None
        )


class TestPrivateContextProbe:
    """Only a context known to be private widens a match to the link set:
    access treats a missing context as private, but here that would widen."""

    @pytest.mark.asyncio
    async def test_private_shared_deleted_and_missing(self, db_session):
        from utils.datetime import utcnow

        admin, _, workspace, private, _ = await _seed(db_session)
        shared = await _shared_context(db_session, workspace, admin)
        service = MemoryService(db_session)

        assert await service._is_private_context(private.id) is True
        assert await service._is_private_context(str(private.id)) is True
        assert await service._is_private_context(shared.id) is False
        assert await service._is_private_context(uuid4()) is False
        assert await service._is_private_context(None) is False
        private.deleted_at = utcnow()
        await db_session.flush()
        assert await service._is_private_context(private.id) is False


async def _upsert(db: AsyncSession, caller: User, context: Context):
    """Run the upsert with remember()/forget() stubbed: what is under test is
    which row the external_id resolves to and what is handed to forget()."""
    service = MemoryService(db)
    remembered = MagicMock(memory_id=uuid4(), scope="working", persistence=None, lint=[])
    with (
        patch.object(service, "remember", AsyncMock(return_value=remembered)),
        patch.object(service, "forget", AsyncMock()) as forget,
    ):
        response = await service.update_memory(
            UpdateMemoryRequest(
                external_id=EXTERNAL_ID,
                summary="the same issue, written from the other account",
                content="updated",
                type="note",
            ),
            user_id=caller.user_id,
            current_context_id=context.id,
            current_workspace_id=context.workspace_id,
        )
    return response, forget


class TestExternalIdUpsert:
    @pytest.mark.asyncio
    async def test_either_linked_account_replaces_the_same_row(self, db_session):
        """The acceptance criterion: oauth's upsert replaces admin's row in
        admin's private context instead of adding a second one."""
        _, oauth, _, context, by_admin = await _seed(db_session)

        response, forget = await _upsert(db_session, oauth, context)

        assert response.operation == "replaced"
        forget.assert_awaited_once()
        assert forget.await_args.args[0].memory_id == by_admin.id
        # forget() authorizes the row itself (can_access_memory: same owner +
        # context EDITOR); the binding row filter is skipped, as for the
        # caller's own row (#1301) — the link makes it the same owner.
        assert forget.await_args.kwargs["_skip_binding_row_filter"] is True

    @pytest.mark.asyncio
    async def test_without_a_link_the_rows_stay_apart(self, db_session):
        _, oauth, _, context, _ = await _seed(db_session, link=False)

        response, forget = await _upsert(db_session, oauth, context)

        assert response.operation == "created"
        forget.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_shared_context_keeps_the_callers_own_rows(self, db_session):
        _, oauth, _, context, _ = await _seed(db_session, private=False)

        response, forget = await _upsert(db_session, oauth, context)

        assert response.operation == "created"
        forget.assert_not_awaited()


async def _declare(db: AsyncSession, caller: User, context: Context, target: Memory) -> set:
    """oauth writes a memory naming ``target`` three ways; returns the edges."""
    new = await _memory(db, context, caller)
    await db.commit()
    request = RememberRequest(
        summary="a newer fact about the same thing",
        content="content",
        type="note",
        linked_memory_ids=[target.id],
        supersedes=target.id,
    )
    service = MemoryService(db)
    await service._create_declared_links(
        memory_id=new.id,
        request=request,
        user_id=caller.user_id,
        workspace_id=str(context.workspace_id),
        context_id=str(context.id),
    )
    by_uri = await _memory(db, context, caller)
    await db.commit()
    await service._create_declared_links(
        memory_id=by_uri.id,
        request=RememberRequest(
            summary="names it by source uri",
            content="content",
            type="note",
            linked_source_uris=[target.source_uri],
        ),
        user_id=caller.user_id,
        workspace_id=str(context.workspace_id),
        context_id=str(context.id),
    )
    rows = await db.execute(
        select(NeuralMemoryEdge.src_id, NeuralMemoryEdge.dst_id, NeuralMemoryEdge.edge_type).where(
            NeuralMemoryEdge.dst_id == target.id
        )
    )
    return {(r.src_id == new.id, r.src_id == by_uri.id, r.edge_type) for r in rows}


class TestDeclaredLinks:
    @pytest.mark.asyncio
    async def test_a_linked_accounts_memory_can_be_named_and_superseded(self, db_session):
        _, oauth, _, context, by_admin = await _seed(db_session)

        edges = await _declare(db_session, oauth, context, by_admin)

        # supersedes upserts over the plain link to the same target.
        assert (True, False, EDGE_TYPE_SUPERSEDES) in edges
        assert any(by_uri for _, by_uri, _ in edges)

    @pytest.mark.asyncio
    async def test_without_a_link_nothing_is_created(self, db_session):
        _, oauth, _, context, by_admin = await _seed(db_session, link=False)

        assert await _declare(db_session, oauth, context, by_admin) == set()

    @pytest.mark.asyncio
    async def test_a_shared_context_keeps_the_callers_own_memories(self, db_session):
        _, oauth, _, context, by_admin = await _seed(db_session, private=False)

        assert await _declare(db_session, oauth, context, by_admin) == set()


class TestPasswordResetNoticeCount:
    @pytest.mark.asyncio
    async def test_counts_the_other_accounts_of_the_set(self, db_session):
        a, b, c = [await _user(db_session) for _ in range(3)]

        assert await sns._linked_account_count(db_session, a.user_id) == 0
        await IdentityLinkService(db_session).link(a.user_id, b.user_id)
        await IdentityLinkService(db_session).link(a.user_id, c.user_id)

        assert await sns._linked_account_count(db_session, a.user_id) == 2
