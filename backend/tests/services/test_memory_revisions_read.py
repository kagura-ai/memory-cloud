"""#2043 PR2: reading the revision log — version reconstruction, list_revisions
and reference(version=N) at the service layer.

``reconstruct_version`` is pure and table-driven here; the service methods run
on a mocked session (the gate is ``reference``'s, pinned by
``test_memory_service_capacity_lock.py``). The MCP handler is in
``tests/mcp_server/test_list_revisions_tool.py``, the REST route in
``tests/api/test_memory_revisions_route.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from models.memory import VERSIONED_FIELDS
from services.memory_service import MemoryService
from services.memory_versions import (
    PRIOR_ERASED_KEY,
    PRIOR_SCRUBBED_KEY,
    UnknownVersionError,
    changed_fields,
    list_revision_rows,
    load_version,
    prior_erased,
    prior_values,
    reconstruct_version,
    revision_item,
)
from utils.exceptions import NotFoundException, ValidationError

T2 = datetime(2026, 10, 2)
T3 = datetime(2026, 10, 3)
T4 = datetime(2026, 10, 4)

# The live row at version 4. History:
#   v1 created: summary "first summary", tags ["a"], context_summary None, importance 0.5
#   v2 edit:    summary -> "second summary", context_summary None -> "why"
#   v3 edit:    tags ["a"] -> ["a", "b"], importance 0.5 -> 0.7
#   v4 edit:    summary -> "fourth summary", context_summary "why" -> None
LIVE: dict[str, Any] = {
    "summary": "fourth summary",
    "context_summary": None,
    "content": "body",
    "details": {"k": "v"},
    "type": "note",
    "importance": 0.7,
    "tags": ["a", "b"],
}


def _rev(version: int, prior: dict[str, Any], *, kind: str = "updated", at: Any = None) -> Any:
    return SimpleNamespace(version=version, kind=kind, prior=prior, at=at)


UPDATES = [
    _rev(4, {"summary": "second summary", "context_summary": "why"}, at=T4),
    _rev(3, {"tags": ["a"], "importance": 0.5}, at=T3),
    _rev(2, {"summary": "first summary", "context_summary": None}, at=T2),
    # other kinds are not edits and never applied
    _rev(1, {}, kind="created"),
    _rev(4, {"summary": "never applied"}, kind="forgotten"),
]


def _expected(**overrides: Any) -> dict[str, Any]:
    return {**LIVE, **overrides}


class TestReconstructVersion:
    @pytest.mark.parametrize(
        ("target", "values", "at"),
        [
            (4, _expected(), T4),  # the current version is the live row
            (3, _expected(summary="second summary", context_summary="why"), T3),
            (
                2,
                _expected(
                    summary="second summary", context_summary="why", tags=["a"], importance=0.5
                ),
                T2,
            ),
            # v2's prior holds context_summary: null ("was null"), not "unchanged"
            (1, _expected(summary="first summary", tags=["a"], importance=0.5), None),
        ],
    )
    def test_each_version_lays_later_priors_over_the_live_row(
        self, target: int, values: dict[str, Any], at: Any
    ) -> None:
        got = reconstruct_version(LIVE, 4, UPDATES, target)
        assert got.version == target
        assert got.values == values
        assert got.unavailable == ()
        assert got.at == at

    def test_rows_arrive_in_any_order(self) -> None:
        shuffled = [UPDATES[2], UPDATES[0], UPDATES[1]]
        assert reconstruct_version(LIVE, 4, shuffled, 1).values["summary"] == "first summary"

    def test_the_live_snapshot_is_not_mutated(self) -> None:
        live = {**LIVE, "tags": ["a", "b"]}
        got = reconstruct_version(live, 4, UPDATES, 1)
        got.values["tags"].append("x")
        assert live["tags"] == ["a", "b"]

    @pytest.mark.parametrize("target", [0, -1, 5])
    def test_a_version_outside_one_to_current_is_unknown(self, target: int) -> None:
        with pytest.raises(UnknownVersionError) as e:
            reconstruct_version(LIVE, 4, UPDATES, target)
        assert e.value.current_version == 4

    def test_a_gap_in_the_log_is_unknown_not_a_wrong_answer(self) -> None:
        with pytest.raises(UnknownVersionError):
            reconstruct_version(LIVE, 4, [UPDATES[0], UPDATES[2]], 1)

    def test_an_erased_value_is_unavailable_never_a_later_one(self) -> None:
        updates = [
            _rev(3, {PRIOR_ERASED_KEY: ["details"]}),  # erasure took the overwritten details
            _rev(2, {"summary": "first summary"}),
        ]
        got = reconstruct_version(LIVE, 3, updates, 2)
        assert got.values["details"] is None
        assert got.unavailable == ("details",)
        # still unavailable further back: no older event names details
        assert reconstruct_version(LIVE, 3, updates, 1).unavailable == ("details",)

    def test_a_scrubbed_live_value_is_unavailable_below_the_marked_event(self) -> None:
        # erasure NULLed the live details; v2 changed only the summary
        updates = [_rev(2, {"summary": "first summary", PRIOR_SCRUBBED_KEY: ["details"]})]
        live = {**LIVE, "details": None}
        got = reconstruct_version(live, 2, updates, 1)
        assert got.values["details"] is None
        assert got.unavailable == ("details",)
        # the marker is not a change of the field
        assert changed_fields(updates[0].prior) == ["summary"]

    def test_an_older_event_that_names_the_field_makes_it_available_again(self) -> None:
        updates = [
            _rev(3, {PRIOR_ERASED_KEY: ["details"]}),
            _rev(2, {"details": {"old": True}}),
        ]
        got = reconstruct_version(LIVE, 3, updates, 1)
        assert got.values["details"] == {"old": True}
        assert got.unavailable == ()


class TestPriorReaders:
    def test_prior_values_drops_the_marker_and_unknown_keys(self) -> None:
        prior = {"summary": "s", PRIOR_ERASED_KEY: ["details"], "bogus": 1}
        assert prior_values(prior) == {"summary": "s"}
        assert prior_values(None) == {}

    def test_prior_erased_reads_versioned_names_only(self) -> None:
        assert prior_erased({PRIOR_ERASED_KEY: ["details", "bogus"]}) == ("details",)
        assert prior_erased({PRIOR_ERASED_KEY: "details"}) == ()
        assert prior_erased({}) == ()

    def test_changed_fields_keeps_registry_order_and_counts_erased(self) -> None:
        prior = {"tags": [], "summary": "s", PRIOR_ERASED_KEY: ["details"]}
        assert changed_fields(prior) == ["summary", "details", "tags"]
        assert set(changed_fields(dict.fromkeys(VERSIONED_FIELDS))) == set(VERSIONED_FIELDS)


def _log_row(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": 7,
        "version": 2,
        "kind": "updated",
        "at": T2,
        "prior": {"summary": "old"},
        "writer": "api",
        "actor_user_id": "someone-else",
        "agent_id": uuid4(),
        "session_id": "sess-1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRevisionItem:
    def test_another_members_event_names_no_one(self) -> None:
        item = revision_item(_log_row(), caller_ids=frozenset({"me"}))
        assert item == {
            "version": 2,
            "kind": "updated",
            "at": T2,
            "changed_fields": ["summary"],
            "writer": "api",
            "by_you": False,
        }

    def test_the_callers_own_event_carries_agent_and_session(self) -> None:
        row = _log_row(actor_user_id="linked-account")
        item = revision_item(row, caller_ids=frozenset({"me", "linked-account"}))
        assert item["by_you"] is True
        assert item["agent_id"] == str(row.agent_id)
        assert item["session_id"] == "sess-1"

    def test_non_edit_events_change_no_fields(self) -> None:
        row = _log_row(kind="forgotten", prior={"summary": "ignored"})
        assert revision_item(row, caller_ids=frozenset())["changed_fields"] == []


class TestQueries:
    @pytest.mark.asyncio
    async def test_list_revision_rows_is_newest_first_keyset_with_one_extra(self) -> None:
        rows = [SimpleNamespace(id=i) for i in (9, 8, 7)]
        anchor = MagicMock()
        anchor.scalar_one_or_none.return_value = 5  # the cursor row's version
        page_result = MagicMock()
        page_result.scalars.return_value.all.return_value = rows
        db = MagicMock(execute=AsyncMock(side_effect=[anchor, page_result]))

        page, has_more = await list_revision_rows(db, uuid4(), before_id=10, limit=2)

        assert [r.id for r in page] == [9, 8]
        assert has_more is True
        lookup, query = (
            str(c.args[0].compile(dialect=postgresql.dialect())) for c in db.execute.await_args_list
        )
        # the cursor is looked up within the same memory
        assert "memory_revisions.id = " in lookup and "memory_revisions.memory_id = " in lookup
        # (version, id) order: an index range read on (memory_id, version, id)
        assert "(memory_revisions.version, memory_revisions.id) < " in query
        assert "ORDER BY memory_revisions.version DESC, memory_revisions.id DESC" in query
        assert "LIMIT" in query

    @pytest.mark.asyncio
    async def test_a_cursor_of_another_memory_is_the_end_of_the_log(self) -> None:
        anchor = MagicMock()
        anchor.scalar_one_or_none.return_value = None
        db = MagicMock(execute=AsyncMock(return_value=anchor))
        assert await list_revision_rows(db, uuid4(), before_id=10, limit=2) == ([], False)
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_first_page_needs_no_lookup(self) -> None:
        page_result = MagicMock()
        page_result.scalars.return_value.all.return_value = []
        db = MagicMock(execute=AsyncMock(return_value=page_result))
        assert await list_revision_rows(db, uuid4(), before_id=None, limit=2) == ([], False)
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_load_version_reads_the_updated_rows_from_the_target_up(self) -> None:
        memory = SimpleNamespace(id=uuid4(), version=4, **LIVE)
        result = MagicMock()
        result.all.return_value = UPDATES[:3]
        db = MagicMock(execute=AsyncMock(return_value=result))

        got = await load_version(db, memory, 2)

        assert got.values["importance"] == 0.5
        sql = str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "memory_revisions.kind = " in sql
        assert "memory_revisions.version >= " in sql

    @pytest.mark.asyncio
    async def test_load_version_refuses_an_unknown_version_without_a_query(self) -> None:
        db = MagicMock(execute=AsyncMock())
        with pytest.raises(UnknownVersionError):
            await load_version(db, SimpleNamespace(id=uuid4(), version=2, **LIVE), 3)
        db.execute.assert_not_awaited()


# ---------------------------------------------------------------- the service


def _memory(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid4(),
        "user_id": "u",
        "workspace_id": uuid4(),
        "context_id": uuid4(),
        "source_type": None,
        "source_uri": None,
        "scope": "working",
        "context": {"file": "x"},
        "client": "api",
        "created_at": datetime(2026, 10, 1),
        "updated_at": T4,
        "version": 4,
        **LIVE,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _service(memory: Any, *, allowed: bool = True) -> tuple[MemoryService, MagicMock]:
    db = MagicMock(commit=AsyncMock(), execute=AsyncMock())
    service = MemoryService(db)
    service.memory_repo = MagicMock(
        get=AsyncMock(return_value=memory), update_access_stats=AsyncMock()
    )
    service._ensure_capacity = AsyncMock()  # type: ignore[method-assign]
    perm = MagicMock(can_access_memory=AsyncMock(return_value=allowed))
    return service, perm


class TestReferenceVersion:
    @pytest.mark.asyncio
    async def test_an_earlier_version_is_rebuilt_without_links_or_access_stats(self) -> None:
        memory = _memory()
        service, perm = _service(memory)
        service._fetch_declared_link_refs = AsyncMock()  # type: ignore[method-assign]
        result = MagicMock()
        result.all.return_value = UPDATES[:3]
        service.db.execute = AsyncMock(return_value=result)

        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()
            ) as emit,
        ):
            ref = await service.reference(memory.id, "u", version=2)

        assert (ref.version, ref.historical, ref.current_version) == (2, True, 4)
        assert ref.summary == "second summary"
        assert ref.tags == ["a"]
        assert ref.updated_at == T2  # when version 2 was written
        assert ref.scope == "working" and ref.context == {"file": "x"}
        assert ref.outgoing_links == [] and ref.supersede_candidate is None
        assert ref.unavailable_fields is None
        service._fetch_declared_link_refs.assert_not_awaited()
        service.memory_repo.update_access_stats.assert_not_awaited()
        emit.assert_awaited_once()
        assert perm.can_access_memory.await_args.kwargs["operation"] == "reference"

    @pytest.mark.asyncio
    async def test_version_one_reads_created_at(self) -> None:
        memory = _memory()
        service, perm = _service(memory)
        result = MagicMock()
        result.all.return_value = UPDATES[:3]
        service.db.execute = AsyncMock(return_value=result)
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
        ):
            ref = await service.reference(memory.id, "u", version=1)
        assert ref.updated_at == memory.created_at

    @pytest.mark.asyncio
    async def test_an_unknown_version_is_a_validation_error_with_the_current_version(
        self,
    ) -> None:
        memory = _memory()
        service, perm = _service(memory)
        with patch("services.permission_service.PermissionService", return_value=perm):
            with pytest.raises(UnknownVersionError) as e:
                await service.reference(memory.id, "u", version=9)
        # a ValidationError: the REST route answers 422 with the current version
        assert isinstance(e.value, ValidationError)
        assert e.value.details == {"field": "version", "current_version": 4}

    @pytest.mark.asyncio
    async def test_a_denied_or_forgotten_memory_is_not_found_before_any_version_check(
        self,
    ) -> None:
        memory = _memory()
        service, perm = _service(memory, allowed=False)
        with patch("services.permission_service.PermissionService", return_value=perm):
            with pytest.raises(NotFoundException):
                await service.reference(memory.id, "u", version=9)
        # repo.get() excludes tombstones: a forgotten memory reads as missing
        service.memory_repo.get = AsyncMock(return_value=None)
        with pytest.raises(NotFoundException):
            await service.reference(memory.id, "u", version=1)


# #2116: version 1 was a "decision"; v2 changed the type to today's "note".
TYPE_CHANGED = [_rev(2, {"type": "decision", "summary": "decision summary"}, at=T2)]


def _type_changed_service(allowed_types: set[str]) -> tuple[Any, MemoryService, MagicMock]:
    """Live type "note" at version 2; version 1 was a "decision". The read
    gate stands in for a binding whose per-memory filter (#1299) allows only
    ``allowed_types``."""
    memory = _memory(version=2, summary="note summary")
    service, perm = _service(memory)

    async def can_access(**kw: Any) -> bool:
        return kw["memory_type"] in allowed_types

    perm.can_access_memory = AsyncMock(side_effect=can_access)
    result = MagicMock()
    result.all.return_value = TYPE_CHANGED
    service.db.execute = AsyncMock(return_value=result)
    return memory, service, perm


def _gated_types(perm: MagicMock) -> list[str]:
    return [c.kwargs["memory_type"] for c in perm.can_access_memory.await_args_list]


class TestReferenceVersionBindingFilter:
    """#2116: the per-memory filter is judged on the historical type too."""

    @pytest.mark.asyncio
    async def test_live_allowed_historical_denied_is_the_uniform_not_found(self) -> None:
        memory, service, perm = _type_changed_service({"note"})
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()
            ) as emit,
        ):
            with pytest.raises(NotFoundException) as e:
                await service.reference(memory.id, "u", version=1)
        # the same exception (and message) the live-row denial raises
        assert str(e.value) == str(NotFoundException("Memory", str(memory.id)))
        assert _gated_types(perm) == ["note", "decision"]  # live first
        kwargs = perm.can_access_memory.await_args.kwargs
        assert kwargs["memory_source_type"] == memory.source_type
        assert (kwargs["operation"], kwargs["memory_id"]) == ("reference", memory.id)
        emit.assert_not_awaited()  # no success audit for a denied read

    @pytest.mark.asyncio
    async def test_live_denied_is_not_found_before_the_historical_check(self) -> None:
        memory, service, perm = _type_changed_service({"decision"})
        with patch("services.permission_service.PermissionService", return_value=perm):
            with pytest.raises(NotFoundException):
                await service.reference(memory.id, "u", version=1)
        assert _gated_types(perm) == ["note"]
        service.db.execute.assert_not_awaited()  # the log was never read

    @pytest.mark.asyncio
    async def test_both_allowed_returns_the_version_text(self) -> None:
        memory, service, perm = _type_changed_service({"note", "decision"})
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
        ):
            ref = await service.reference(memory.id, "u", version=1)
        assert (ref.type, ref.summary, ref.version) == ("decision", "decision summary", 1)

    @pytest.mark.asyncio
    async def test_an_unchanged_type_keeps_the_live_verdict(self) -> None:
        memory = _memory()
        service, perm = _service(memory)
        result = MagicMock()
        result.all.return_value = UPDATES[:3]
        service.db.execute = AsyncMock(return_value=result)
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
        ):
            ref = await service.reference(memory.id, "u", version=2)
        assert ref.summary == "second summary"
        perm.can_access_memory.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unavailable_historical_type_is_denied(self) -> None:
        # never today (erasure marks only details): the filter cannot be judged
        memory, service, perm = _type_changed_service({"note", "decision"})
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch(
                "services.memory_versions.reconstruct_version",
                return_value=SimpleNamespace(
                    version=1, values={**LIVE, "type": None}, unavailable=("type",), at=None
                ),
            ),
        ):
            with pytest.raises(NotFoundException):
                await service.reference(memory.id, "u", version=1)


class TestReferenceVersionRealBindingGate:
    """#2116 through the real PermissionService and AgentBindingService: an
    agent bound to the memory's context with ``allowed_memory_types=["note"]``.
    Only the seat lookup and the DB reads are stubbed."""

    async def _read(self, mode: str) -> tuple[Any, AsyncMock]:
        from auth.agent_scope import AgentScope
        from models.agent import AgentContextBinding

        memory = _memory(version=2, summary="note summary")
        service, _ = _service(memory)
        binding = AgentContextBinding(
            id=uuid4(),
            agent_id=uuid4(),
            context_id=memory.context_id,
            can_read=True,
            write_policy="deny",
            is_default=False,
            allowed_memory_types=["note"],
            allowed_source_types=None,
            created_by="u",
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = binding  # the binding SELECT
        result.all.return_value = TYPE_CHANGED  # the revision log
        service.db.execute = AsyncMock(return_value=result)
        scope = AgentScope(agent_id=binding.agent_id, enforcement_mode=mode, workspace_id=uuid4())
        with (
            patch("auth.agent_scope.get_agent_scope", return_value=scope),
            patch(
                "services.permission_service.PermissionService._seated_member",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()
            ) as emit,
        ):
            try:
                return await service.reference(memory.id, "u", version=1), emit
            except NotFoundException as e:
                return e, emit

    @pytest.mark.asyncio
    async def test_enforce_denies_with_a_type_source_binding_denied_event(self) -> None:
        from services.agent_binding_service import DECISION_BINDING_DENIED, ROW_FILTER_KIND

        outcome, emit = await self._read("enforce")
        assert isinstance(outcome, NotFoundException)
        (call,) = emit.await_args_list  # the deny; no success audit
        assert call.kwargs["outcome"] == "denied"
        assert call.kwargs["operation"] == "reference"
        assert call.kwargs["policy_decision"] == DECISION_BINDING_DENIED
        assert call.kwargs["extra_metadata"]["filter_kind"] == ROW_FILTER_KIND

    @pytest.mark.asyncio
    async def test_shadow_serves_the_version_and_records_would_deny(self) -> None:
        from services.agent_binding_service import DECISION_WOULD_DENY

        outcome, emit = await self._read("shadow")
        assert outcome.summary == "decision summary"
        decisions = [c.kwargs.get("policy_decision") for c in emit.await_args_list]
        assert DECISION_WOULD_DENY in decisions


class TestListRevisions:
    @pytest.mark.asyncio
    async def test_a_page_with_its_cursor_and_the_callers_identity_set(self) -> None:
        memory = _memory()
        service, perm = _service(memory)
        rows = [_log_row(id=9, version=4), _log_row(id=8, version=3, actor_user_id="u")]
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch(
                "services.memory_versions.list_revision_rows",
                new=AsyncMock(return_value=(rows, True)),
            ) as page_rows,
            patch(
                "services.identity_link_service.linked_user_ids",
                new=AsyncMock(return_value=frozenset({"u"})),
            ),
        ):
            page = await service.list_revisions(memory.id, "u", before_id=12, limit=2)

        assert page.current_version == 4
        assert [r.version for r in page.revisions] == [4, 3]
        assert [r.by_you for r in page.revisions] == [False, True]
        assert page.has_more is True
        assert page.next_cursor == "8"
        assert page.revisions[0]._cursor == "9"
        assert page_rows.await_args.kwargs == {"before_id": 12, "limit": 2}
        service._ensure_capacity.assert_awaited_once()
        service.memory_repo.update_access_stats.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_last_page_has_no_cursor(self) -> None:
        memory = _memory()
        service, perm = _service(memory)
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch(
                "services.memory_versions.list_revision_rows",
                new=AsyncMock(return_value=([_log_row()], False)),
            ),
            patch(
                "services.identity_link_service.linked_user_ids",
                new=AsyncMock(return_value=frozenset({"u"})),
            ),
        ):
            page = await service.list_revisions(memory.id, "u")
        assert (page.has_more, page.next_cursor) == (False, None)

    @pytest.mark.asyncio
    async def test_a_memory_outside_the_named_context_is_not_found(self) -> None:
        memory = _memory()
        service, perm = _service(memory)
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch("services.memory_versions.list_revision_rows", new=AsyncMock()) as page_rows,
        ):
            with pytest.raises(NotFoundException):
                await service.list_revisions(memory.id, "u", context_id=uuid4())
        page_rows.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_denied_is_not_found_and_reads_no_log(self) -> None:
        memory = _memory()
        service, perm = _service(memory, allowed=False)
        with (
            patch("services.permission_service.PermissionService", return_value=perm),
            patch("services.memory_versions.list_revision_rows", new=AsyncMock()) as page_rows,
        ):
            with pytest.raises(NotFoundException):
                await service.list_revisions(memory.id, "u")
        page_rows.assert_not_awaited()
        service._ensure_capacity.assert_not_awaited()
