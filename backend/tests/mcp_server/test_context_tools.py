"""Tests for MCP context tool handlers (Issue #401).

Locks down the AuthorizationError / NotFoundException response surface for
``handle_delete_context``. The branches at ``mcp_server/tools/context.py``
lines 691 (NotFoundException → ``context_not_found``) and 697-700
(AuthorizationError → ``permission_denied``) were dead code before #401:
``PermissionService.check_context_owner`` raised ``HTTPException``, which
matched neither domain-exception branch and fell into the generic 500
``delete_context_error`` path.

After #401 the service raises domain exceptions and these branches are live
— this test pins that contract so a future refactor of the MCP error
surface can't silently revert it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.constants import GATE_PLAN
from mcp_server.tools._helpers import _ContextNotFoundError
from mcp_server.tools.context import (
    handle_create_context,
    handle_delete_context,
    handle_merge_contexts,
    handle_update_context,
)
from utils.exceptions import (
    AuthorizationError,
    FeatureNotAvailableError,
    NotFoundException,
    ValidationError,
)


class TestHandleDeleteContextErrorSurface:
    @pytest.fixture
    def user_id(self):
        return "test_user_401"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_authorization_error_returns_permission_denied(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner AuthorizationError → MCP ``permission_denied``.

        Issue #401: this branch became live when PermissionService swapped
        to domain exceptions. Before the refactor it was dead — the
        HTTPException(403) raised by check_context_owner fell into the
        generic Exception 500 path instead.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=AuthorizationError("Insufficient permissions")
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_delete_context(
                args={"context_id": str(context_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "permission_denied"
        mock_db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_not_found_exception_returns_context_not_found(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner NotFoundException → MCP ``context_not_found``.

        Same uniform-disclosure contract as test_graph_visibility's 404
        cases: regardless of whether the context truly doesn't exist or
        the caller can't see it, the MCP surface emits ``context_not_found``.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=NotFoundException("Context", str(context_id))
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_delete_context(
                args={"context_id": str(context_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_not_found"
        mock_db.rollback.assert_awaited()


class TestHandleMergeContextsWorkspaceBoundary:
    """Issue #966: ``handle_merge_contexts`` must apply the workspace-boundary
    guard at the MCP boundary (mirror of ``handle_recall``), resolving both
    source and target via ``_resolve_context_for_read`` and rejecting a
    cross-workspace merge with a uniform ``workspace_mismatch`` error before
    ``ContextService.merge_contexts`` is ever called.

    Without the guard a member of two workspaces could probe / merge across
    the workspace boundary, and an access-denied context leaks through the
    generic ``merge_contexts_error`` 500 envelope (CWE-639) instead of the
    uniform ``context_not_found`` shape.
    """

    @pytest.fixture
    def user_id(self):
        return "test_user_966"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def source_id(self):
        return uuid4()

    @pytest.fixture
    def target_id(self):
        return uuid4()

    def _ctx(self, workspace_id):
        ctx = MagicMock()
        ctx.workspace_id = workspace_id
        return ctx

    @pytest.mark.asyncio
    async def test_cross_workspace_merge_returns_workspace_mismatch(
        self, user_id, workspace_id, source_id, target_id
    ):
        """Source and target in different workspaces → ``workspace_mismatch``,
        and ``merge_contexts`` is never invoked."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        source_ctx = self._ctx(uuid4())
        target_ctx = self._ctx(uuid4())  # different workspace

        resolve = AsyncMock(side_effect=[source_ctx, target_ctx])
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.context._resolve_context_for_read",
                new=resolve,
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_service,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_merge_contexts(
                args={
                    "source_context_id": str(source_id),
                    "target_context_id": str(target_id),
                },
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "workspace_mismatch"
        mock_service.merge_contexts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreadable_context_returns_context_not_found(
        self, user_id, workspace_id, source_id, target_id
    ):
        """A context the caller can't read → uniform ``context_not_found``
        (not the generic ``merge_contexts_error`` 500 leak), and
        ``merge_contexts`` is never invoked."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        resolve = AsyncMock(
            side_effect=_ContextNotFoundError(
                source_id, "Context not found or you don't have access to it."
            )
        )
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.context._resolve_context_for_read",
                new=resolve,
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_service,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_merge_contexts(
                args={
                    "source_context_id": str(source_id),
                    "target_context_id": str(target_id),
                },
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_not_found"
        mock_service.merge_contexts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_workspace_proceeds_to_merge(
        self, user_id, workspace_id, source_id, target_id
    ):
        """Both contexts in the same workspace → guard passes and
        ``merge_contexts`` runs."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        shared_ws = uuid4()
        resolve = AsyncMock(side_effect=[self._ctx(shared_ws), self._ctx(shared_ws)])
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock(return_value={"merged": 3, "source_deleted": False})

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.context._resolve_context_for_read",
                new=resolve,
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_service,
            ),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_merge_contexts(
                args={
                    "source_context_id": str(source_id),
                    "target_context_id": str(target_id),
                },
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "success"
        assert payload["merged"] == 3
        mock_service.merge_contexts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deprecated_source_id_alias_still_merges(
        self, user_id, workspace_id, source_id, target_id
    ):
        """#990: the renamed params advertise source_context_id/target_context_id,
        but the handler still accepts the old source_id/target_id for one release
        (deprecated alias) so the current SDK keeps working."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        shared_ws = uuid4()
        resolve = AsyncMock(side_effect=[self._ctx(shared_ws), self._ctx(shared_ws)])
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock(return_value={"merged": 1, "source_deleted": False})

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch("mcp_server.tools.context._resolve_context_for_read", new=resolve),
            patch("services.context_service.ContextService", return_value=mock_service),
            patch("mcp_server.tools.context._log_tool_usage", new_callable=AsyncMock),
        ):
            result = await handle_merge_contexts(
                args={"source_id": str(source_id), "target_id": str(target_id)},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "success"
        mock_service.merge_contexts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_both_param_names_errors(self, user_id, workspace_id):
        """Neither new nor old source/target keys → missing_fields with the new
        canonical field names in the message (#990)."""
        result = await handle_merge_contexts(args={}, user_id=user_id, workspace_id=workspace_id)
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "missing_fields"
        assert "source_context_id" in payload["message"]

    @pytest.mark.asyncio
    async def test_failure_path_logs_canonical_source_id(
        self, user_id, workspace_id, source_id, target_id
    ):
        """#990 regression: the 500-error usage log must record the source
        context id even for the new canonical source_context_id name. A naive
        args.get("source_id") would log None on the failure path, losing the
        audit trail for exactly the calls the rename standardizes on."""
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        shared_ws = uuid4()
        resolve = AsyncMock(side_effect=[self._ctx(shared_ws), self._ctx(shared_ws)])
        mock_service = MagicMock()
        mock_service.merge_contexts = AsyncMock(side_effect=RuntimeError("boom"))
        log_mock = AsyncMock()

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch("mcp_server.tools.context._resolve_context_for_read", new=resolve),
            patch("services.context_service.ContextService", return_value=mock_service),
            patch("mcp_server.tools.context._log_tool_usage", new=log_mock),
        ):
            result = await handle_merge_contexts(
                args={
                    "source_context_id": str(source_id),
                    "target_context_id": str(target_id),
                },
                user_id=user_id,
                workspace_id=workspace_id,
            )

        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "merge_contexts_error"
        # resource_id is the 6th positional arg of _log_tool_usage; must be the
        # canonical source id, never None.
        log_mock.assert_awaited_once()
        assert log_mock.await_args.args[5] == str(source_id)


class TestHandleUpdateContextErrorSurface:
    """Pins the domain-exception envelope contract for handle_update_context
    (mirror of handle_delete_context). AuthorizationError → permission_denied
    with exc.message (CWE-639 uniform string). NotFoundException →
    context_not_found.
    """

    @pytest.fixture
    def user_id(self):
        return "test_user_604"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_authorization_error_returns_permission_denied(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner AuthorizationError → MCP ``permission_denied``.

        The response message must come from ``exc.message`` (the
        AuthorizationError-enforced uniform ``"Insufficient permissions"``),
        not ``str(exc)`` which could vary across str-coercion edge cases.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=AuthorizationError("Insufficient permissions")
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
        ):
            result = await handle_update_context(
                args={"context_id": str(context_id), "summary": "new summary"},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "permission_denied"
        assert payload["message"] == "Insufficient permissions"
        mock_db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_not_found_exception_returns_context_not_found(
        self, user_id, workspace_id, context_id
    ):
        """check_context_owner NotFoundException → MCP ``context_not_found``.

        Same uniform-disclosure contract as the delete-path test: regardless
        of whether the context truly doesn't exist or the caller can't see
        it, the MCP surface emits ``context_not_found``.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        mock_perm = MagicMock()
        mock_perm.check_context_owner = AsyncMock(
            side_effect=NotFoundException("Context", str(context_id))
        )

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "services.permission_service.PermissionService",
                return_value=mock_perm,
            ),
        ):
            result = await handle_update_context(
                args={"context_id": str(context_id), "summary": "new summary"},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["status"] == "error"
        assert payload["error"] == "context_not_found"
        mock_db.rollback.assert_awaited()


def _get_db_yielding(db):
    async def _get_db():
        yield db

    return _get_db


class TestHandleGetContextInfoGuardrails:
    """#1621: the ``guardrails`` block — the session-start guardrail lane for
    MCP clients without tool hooks. Absent on ``?guardrails=off``, ``null``
    when the read fails, otherwise the trusted-only tool-triggered set capped
    at 10 × 300 / 4,000 characters of compact JSON. The block reuses the
    resolved context and one repo read: never the embedding client or the
    vector store, never a second permission read."""

    @pytest.fixture(autouse=True)
    def _clear_selection(self):
        from mcp_server.tools._helpers import set_mcp_guardrails_selection

        set_mcp_guardrails_selection(None)
        yield
        set_mcp_guardrails_selection(None)

    @staticmethod
    def _context(ctx_id):
        from types import SimpleNamespace

        return SimpleNamespace(
            id=ctx_id,
            name="dev",
            display_name="Dev",
            summary="s",
            usage_guide="g",
            is_private=False,
            is_locked=False,
            workspace_id=uuid4(),
        )

    @staticmethod
    def _db():
        db = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=exec_result)
        return db

    @staticmethod
    def _stats_service():
        from types import SimpleNamespace

        stats = SimpleNamespace(
            total_count=1,
            working_count=0,
            persistent_count=1,
            by_type={},
            by_importance={},
            recent_activity={},
        )
        service = MagicMock()
        service.get_stats = AsyncMock(return_value=stats)
        return service

    @staticmethod
    def _entries(ctx_id, *summaries, total=None, authored=True):
        from services.guardrail_digest import DigestEntries, DigestEntry

        items = [
            DigestEntry(
                memory_id=str(uuid4()),
                summary=s,
                importance=0.7,
                authored_by_caller=authored,
                source_type="manual",
            )
            for s in summaries
        ]
        total = len(items) if total is None else total
        return DigestEntries(
            context_id=ctx_id,
            entries=items,
            total_available=total,
            truncated=total > len(items),
            tool_triggered_version="0123456789abcdef",
        )

    async def _call(self, ctx, db, fetch):
        with (
            patch("db.base.get_db", new=_get_db_yielding(db)),
            patch(
                "mcp_server.tools.context._resolve_context_for_read",
                new=AsyncMock(return_value=ctx),
            ),
            patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
            patch(
                "services.memory_service.MemoryService",
                new=MagicMock(return_value=self._stats_service()),
            ),
            patch("services.guardrail_digest.fetch_entries_for_context", new=fetch),
        ):
            from mcp_server.tools.context import handle_get_context_info

            result = await handle_get_context_info(
                {"context_id": str(ctx.id)}, user_id="u1", workspace_id=None
            )
        return json.loads(result[0].text)

    @pytest.mark.asyncio
    async def test_block_holds_the_entry_source_rows_with_the_documented_shape(self):
        ctx = self._context(uuid4())
        entries = self._entries(ctx.id, "one", "two", total=2)
        fetch = AsyncMock(return_value=entries)

        payload = await self._call(ctx, self._db(), fetch)

        assert payload["status"] == "success"
        block = payload["guardrails"]
        assert set(block) == {
            "items",
            "total_available",
            "truncated",
            "tool_triggered_version",
        }
        assert "version" not in block
        assert block["tool_triggered_version"] == "0123456789abcdef"
        assert block["total_available"] == 2 and block["truncated"] is False
        assert [i["summary"] for i in block["items"]] == ["one", "two"]
        assert set(block["items"][0]) == {
            "memory_id",
            "summary",
            "importance",
            "authored_by_caller",
            "source_type",
        }
        # The resolved Context is reused — no second permission read, cap 10.
        kwargs = fetch.await_args.kwargs
        assert kwargs["context"] is ctx and kwargs["limit"] == 10 and kwargs["user_id"] == "u1"
        # The rest of the result is untouched.
        assert payload["context"]["id"] == str(ctx.id)
        assert payload["stats"]["total_memories"] == 1
        assert list(payload) == [
            "status",
            "context",
            "workspace",
            "stats",
            "guardrails",
            "instructions",
        ]

    @pytest.mark.asyncio
    async def test_block_is_capped_at_ten_items_and_flags_truncation(self):
        from mcp_server.tools._helpers import _dumps

        ctx = self._context(uuid4())
        entries = self._entries(ctx.id, *[f"lesson {i} " + "x" * 400 for i in range(12)], total=12)

        payload = await self._call(ctx, self._db(), AsyncMock(return_value=entries))

        block = payload["guardrails"]
        assert len(block["items"]) <= 10
        assert all(len(i["summary"]) <= 300 for i in block["items"])
        assert block["truncated"] is True and block["total_available"] == 12
        assert len(_dumps(block)) <= 4_000

    @pytest.mark.asyncio
    async def test_external_tier_or_unmarked_context_is_an_empty_block(self):
        ctx = self._context(uuid4())
        entries = self._entries(ctx.id)

        payload = await self._call(ctx, self._db(), AsyncMock(return_value=entries))

        assert payload["guardrails"] == {
            "items": [],
            "total_available": 0,
            "truncated": False,
            "tool_triggered_version": "0123456789abcdef",
        }

    @pytest.mark.asyncio
    async def test_entry_source_failure_is_null_and_the_result_still_succeeds(self, caplog):
        ctx = self._context(uuid4())
        db = self._db()
        fetch = AsyncMock(side_effect=RuntimeError("statement timeout"))

        with caplog.at_level("WARNING", logger="mcp_server.tools.context"):
            payload = await self._call(ctx, db, fetch)

        assert payload["status"] == "success"
        assert payload["guardrails"] is None  # unknown, distinguishable from "none"
        assert payload["context"]["id"] == str(ctx.id)
        assert payload["stats"]["total_memories"] == 1
        # The shared session is rolled back so the assembled result goes out.
        db.rollback.assert_awaited()
        assert "get_context_info_guardrails_failed" in caplog.text
        assert "reason=RuntimeError" in caplog.text

    @pytest.mark.asyncio
    async def test_guardrails_off_on_the_url_removes_the_key(self):
        from mcp_server.tools._helpers import set_mcp_guardrails_selection
        from services.guardrail_digest import select_guardrail_context

        set_mcp_guardrails_selection(select_guardrail_context(b"guardrails=off"))
        ctx = self._context(uuid4())
        fetch = AsyncMock(return_value=self._entries(ctx.id, "hidden"))

        payload = await self._call(ctx, self._db(), fetch)

        assert "guardrails" not in payload
        fetch.assert_not_awaited()
        assert payload["status"] == "success"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            b"",
            b"guardrails=not-a-uuid",
            b"guardrails=550e8400-e29b-41d4-a716-446655440000",
        ],
    )
    async def test_every_other_selection_keeps_the_block_on_for_the_calls_own_context(self, query):
        """``ignored`` (a typo) must never behave like ``off``; ``explicit``
        selects the ``instructions`` digest but the block stays per call."""
        from mcp_server.tools._helpers import set_mcp_guardrails_selection
        from services.guardrail_digest import select_guardrail_context

        set_mcp_guardrails_selection(select_guardrail_context(query))
        ctx = self._context(uuid4())
        fetch = AsyncMock(return_value=self._entries(ctx.id, "shown"))

        payload = await self._call(ctx, self._db(), fetch)

        assert payload["guardrails"]["items"][0]["summary"] == "shown"
        assert fetch.await_args.kwargs["context"] is ctx  # the call's context, not the URL's

    @pytest.mark.asyncio
    async def test_block_never_calls_the_embedding_client_or_the_vector_store(self, monkeypatch):
        """The real entry source runs over a fake repo: ``get_context_info`` is
        rate-limit exempt, so the extra read must stay one indexed SQL query."""
        from types import SimpleNamespace

        from repositories.memory import MemoryRepository

        ctx = self._context(uuid4())
        rows = [
            SimpleNamespace(
                id=uuid4(),
                summary="from the repo gate",
                importance=0.9,
                delivery_mode="on_recall",
                tool_trigger={"tool": "Bash", "on": "pre", "action": "inform"},
                user_id="u1",
                source_type="manual",
                type="note",
                context_id=ctx.id,
            )
        ]
        seen: dict = {}

        async def fake_list(self, workspace_id, context_id, limit):
            seen.update(workspace_id=workspace_id, context_id=context_id, limit=limit)
            return rows, 1

        monkeypatch.setattr(MemoryRepository, "list_tool_triggered", fake_list)
        boom = AsyncMock(side_effect=AssertionError("must not be called on the guardrail lane"))

        from services.guardrail_digest import fetch_entries_for_context

        with (
            patch("db.qdrant.search_memories_qdrant", new=boom),
            patch("services.embedding_service.EmbeddingService.embed", new=boom),
            patch("services.embedding_service.EmbeddingService.embed_with_usage", new=boom),
        ):
            payload = await self._call(ctx, self._db(), fetch_entries_for_context)

        from config.settings import get_settings

        assert payload["guardrails"]["items"][0]["summary"] == "from the repo gate"
        # One read, bounded by the larger of the lane cap (10) and the clamped
        # ``guardrail_load_cap`` so ``tool_triggered_version`` covers the set.
        assert seen == {
            "workspace_id": ctx.workspace_id,
            "context_id": ctx.id,
            "limit": max(10, get_settings().guardrail_load_cap),
        }
        boom.assert_not_awaited()


class TestHandleCreateContextPlanRefusal:
    """#1644 S11: a plan refusal from ``create_context`` is ``plan_required``.

    The generic catch-all in ``handle_create_context`` classifies errors by
    the exception class NAME (``"ValidationError" in type(e).__name__``). The
    shared-context gate used to raise ``ValidationError``, so it landed in the
    ``validation_error`` arm by accident of naming. S11 makes it raise
    ``FeatureNotAvailableError``, which matches no name test — without the
    explicit branch the refusal would be reported as ``create_context_error``,
    a generic failure carrying no machine-readable reason.
    """

    @pytest.fixture
    def user_id(self):
        return "test_user_1644"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @staticmethod
    async def _create_shared(user_id, workspace_id, side_effect):
        """Drive ``handle_create_context`` to the point the service refuses.

        Everything before the service call (role, quota, embedding model) is
        stubbed green so the only thing under test is how the handler
        classifies what ``create_context`` raised.
        """
        mock_db = AsyncMock()
        mock_db.rollback = AsyncMock()
        service = MagicMock()
        service.create_context = AsyncMock(side_effect=side_effect)

        async def mock_get_db():
            yield mock_db

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.context._get_workspace_member_role",
                new=AsyncMock(return_value="owner"),
            ),
            patch(
                "services.quota_service.QuotaService.check_context_creation_allowed",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch("services.context_service.ContextService", return_value=service),
            patch(
                "mcp_server.tools.context._log_tool_usage",
                new_callable=AsyncMock,
            ),
        ):
            result = await handle_create_context(
                args={"name": "team-ctx", "is_private": False},
                user_id=user_id,
                workspace_id=workspace_id,
            )

        assert len(result) == 1
        return json.loads(result[0].text), mock_db

    @pytest.mark.asyncio
    async def test_feature_refusal_is_plan_required_not_a_generic_error(
        self, user_id, workspace_id
    ):
        payload, mock_db = await self._create_shared(
            user_id,
            workspace_id,
            FeatureNotAvailableError.for_feature("free", "shared_contexts"),
        )

        assert payload["status"] == "error"
        assert payload["error"] == "plan_required"
        assert payload["error"] != "create_context_error"
        mock_db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_plan_required_carries_the_whole_details_block(self, user_id, workspace_id):
        """Same ``**exc.details`` splat ``setup_connector`` uses, so an MCP
        client reads one vocabulary for "upgrade to create this"."""
        payload, _ = await self._create_shared(
            user_id,
            workspace_id,
            FeatureNotAvailableError.for_feature("free", "shared_contexts"),
        )

        assert payload["gate"] == GATE_PLAN
        assert payload["feature"] == "shared_contexts"
        assert payload["required_plan"] == "pro"
        assert payload["current_plan"] == "free"

    @pytest.mark.asyncio
    async def test_validation_errors_still_reach_the_validation_arm(self, user_id, workspace_id):
        """Back-compat: the new branch must not swallow the old one."""
        payload, _ = await self._create_shared(
            user_id, workspace_id, ValidationError("Context name cannot be empty")
        )

        assert payload["error"] == "validation_error"
        assert payload["help"] == "Check the context name and try again."
