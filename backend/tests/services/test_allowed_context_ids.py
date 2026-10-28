"""The member context access list (``WorkspaceMember.allowed_context_ids``).

One rule (Migration 042), in ``auth.workspace_roles``: owner/admin reach every
context; a member or viewer reaches exactly what its list names; with no list
a viewer reaches every shared context and a member none. A role change keeps
the list, except that a member with no list who becomes a viewer gets ``[]``.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from auth.workspace_roles import WorkspaceRole, reachable_context_ids, reaches_context
from services.permission_service import reach_filter
from services.workspace_service import WorkspaceService

X = uuid.UUID("00000000-0000-0000-0000-00000000000a")
Y = uuid.UUID("00000000-0000-0000-0000-00000000000b")

LISTS = {"null": None, "empty": [], "x": [X], "y": [Y]}

# role -> list -> does it reach context X?
EXPECTED = {
    WorkspaceRole.OWNER: {"null": True, "empty": True, "x": True, "y": True},
    WorkspaceRole.ADMIN: {"null": True, "empty": True, "x": True, "y": True},
    WorkspaceRole.MEMBER: {"null": False, "empty": False, "x": True, "y": False},
    WorkspaceRole.VIEWER: {"null": True, "empty": False, "x": True, "y": False},
    "guest": {"null": False, "empty": False, "x": False, "y": False},
}


class TestReachesContext:
    @pytest.mark.parametrize("role", list(EXPECTED))
    @pytest.mark.parametrize("list_name", list(LISTS))
    def test_table(self, role, list_name) -> None:
        assert reaches_context(role, LISTS[list_name], X) is EXPECTED[role][list_name]

    @pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
    def test_plain_role_strings_match_the_enum(self, role) -> None:
        for list_name, allowed in LISTS.items():
            assert reaches_context(role, allowed, X) is EXPECTED[WorkspaceRole(role)][list_name]

    def test_a_string_id_is_compared_as_its_uuid(self) -> None:
        assert reaches_context(WorkspaceRole.MEMBER, [X], str(X)) is True
        assert reaches_context(WorkspaceRole.MEMBER, [X], str(X).upper()) is True
        assert reaches_context(WorkspaceRole.MEMBER, [X], str(Y)) is False
        assert reaches_context(WorkspaceRole.MEMBER, [X], "not-a-uuid") is False

    def test_reachable_ids_shape(self) -> None:
        assert reachable_context_ids(WorkspaceRole.OWNER, []) is None
        assert reachable_context_ids(WorkspaceRole.VIEWER, None) is None
        assert reachable_context_ids(WorkspaceRole.MEMBER, None) == frozenset()
        assert reachable_context_ids(WorkspaceRole.VIEWER, [X, X]) == frozenset({X})
        assert reachable_context_ids("guest", [X]) == frozenset()


class TestReachFilter:
    def _sql(self, clause) -> str:
        return str(
            clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    def test_no_filter_for_every_context(self) -> None:
        assert (
            reach_filter(SimpleNamespace(role=WorkspaceRole.ADMIN, allowed_context_ids=[])) is None
        )
        assert (
            reach_filter(SimpleNamespace(role=WorkspaceRole.VIEWER, allowed_context_ids=None))
            is None
        )

    def test_nothing_for_an_unset_member_or_an_empty_list(self) -> None:
        for member in (
            SimpleNamespace(role=WorkspaceRole.MEMBER, allowed_context_ids=None),
            SimpleNamespace(role=WorkspaceRole.VIEWER, allowed_context_ids=[]),
        ):
            assert self._sql(reach_filter(member)) == "false"

    def test_exactly_the_list(self) -> None:
        sql = self._sql(reach_filter(SimpleNamespace(role="member", allowed_context_ids=[X])))
        assert "contexts.id IN" in sql and str(X) in sql and str(Y) not in sql


class TestRoleChangeKeepsTheList:
    """``WorkspaceService.update_member_role`` keeps the stored list."""

    async def _change(self, old_role, allowed, new_role):
        db = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=lambda: [])))
        )
        service = WorkspaceService(db)
        member = SimpleNamespace(
            role=old_role, allowed_context_ids=allowed, user_id="u", updated_at=None
        )
        service.get_member = AsyncMock(return_value=member)
        await service.update_member_role(uuid.uuid4(), "u", new_role)
        return member

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("old_role", "new_role"),
        [
            ("member", "viewer"),
            ("viewer", "member"),
            ("member", "admin"),
            ("admin", "member"),
            ("viewer", "admin"),
            ("admin", "viewer"),
        ],
    )
    async def test_a_list_survives(self, old_role, new_role) -> None:
        member = await self._change(old_role, [X], new_role)
        assert member.role == new_role
        assert member.allowed_context_ids == [X]

    @pytest.mark.asyncio
    async def test_an_unset_member_becomes_a_viewer_with_no_context(self) -> None:
        member = await self._change("member", None, "viewer")
        assert member.allowed_context_ids == []
        assert not reaches_context(member.role, member.allowed_context_ids, X)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("old_role", "new_role"),
        [("viewer", "member"), ("admin", "member"), ("member", "admin"), ("admin", "viewer")],
    )
    async def test_an_unset_list_stays_unset(self, old_role, new_role) -> None:
        member = await self._change(old_role, None, new_role)
        assert member.allowed_context_ids is None


class TestReachableMemoryFilter:
    """``reachable_memory_filter`` (#2325): the caller's reach over its own
    memories with no context given, one predicate from every membership."""

    WS1 = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")

    @staticmethod
    def _row(ws_id, role, allowed):
        return (
            SimpleNamespace(role=role, allowed_context_ids=allowed, user_id="u"),
            SimpleNamespace(id=ws_id),
        )

    @staticmethod
    def _db(rows):
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        return db

    @staticmethod
    def _sql(clause) -> str:
        return str(
            clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    async def _filter(self, rows, *, unseated=(), **kw):
        from services.permission_service import reachable_memory_filter

        async def suspended(_db, workspace, _member):
            return workspace.id in unseated

        with patch("services.permission_service.member_seat_suspended", side_effect=suspended):
            return self._sql(await reachable_memory_filter(self._db(rows), "u", **kw))

    @pytest.mark.asyncio
    async def test_nothing_without_a_membership_that_reaches(self) -> None:
        assert await self._filter([]) == "false"
        rows = [self._row(self.WS1, WorkspaceRole.MEMBER, None), self._row(self.WS2, "viewer", [])]
        assert await self._filter(rows) == "false"

    @pytest.mark.asyncio
    async def test_every_context_of_an_open_workspace(self) -> None:
        rows = [
            self._row(self.WS1, WorkspaceRole.OWNER, [X]),
            self._row(self.WS2, WorkspaceRole.VIEWER, None),
        ]
        sql = await self._filter(rows)
        assert sql == f"memories.workspace_id IN ('{self.WS1}', '{self.WS2}')"

    @pytest.mark.asyncio
    async def test_exactly_the_list_in_its_workspace(self) -> None:
        rows = [self._row(self.WS1, "member", [X]), self._row(self.WS2, WorkspaceRole.ADMIN, None)]
        sql = await self._filter(rows)
        assert f"memories.workspace_id = '{self.WS1}' AND memories.context_id IN" in sql
        assert str(X) in sql and str(Y) not in sql
        assert f"memories.workspace_id IN ('{self.WS2}')" in sql

    @pytest.mark.asyncio
    async def test_a_member_without_a_seat_reaches_nothing_there(self) -> None:
        """#1943: the context resolvers refuse an unseated member; so does this."""
        rows = [self._row(self.WS1, "member", [X]), self._row(self.WS2, "admin", None)]
        assert await self._filter(rows, unseated={self.WS1}) == (
            f"memories.workspace_id IN ('{self.WS2}')"
        )
        assert await self._filter(rows, unseated={self.WS1, self.WS2}) == "false"

    @pytest.mark.asyncio
    async def test_memberships_are_read_for_the_caller_in_live_workspaces(self) -> None:
        from services.permission_service import reachable_memory_filter

        db = self._db([])
        await reachable_memory_filter(db, "u", key_workspace_id=self.WS1)
        stmt = self._sql(db.execute.await_args.args[0])
        assert "workspace_members.user_id = 'u'" in stmt
        assert "workspaces.deleted_at IS NULL" in stmt
        # A workspace-scoped API key reaches its own workspace only (#963).
        assert f"workspace_members.workspace_id = '{self.WS1}'" in stmt
        db = self._db([])
        await reachable_memory_filter(db, "u")
        assert "workspace_members.workspace_id =" not in self._sql(db.execute.await_args.args[0])
