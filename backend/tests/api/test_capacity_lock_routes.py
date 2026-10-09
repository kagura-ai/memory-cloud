"""The capacity-over lock on the REST routes outside MemoryService (#1941).

Each route group is driven with its collaborators mocked and the route
module's lock function patched to "locked", which pins WHERE the check sits:
after the existing access check, before the work. The decisions mirror the
MCP classification (``mcp_server/tools/_capacity_gate.py``): content reads and
writes are refused; deleting, cancelling and listing metadata are not.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from utils.exceptions import CapacityLockedError


def _locked() -> CapacityLockedError:
    return CapacityLockedError(
        memory_count=1001,
        memory_limit=1000,
        over_memories=1,
        used_bytes=0,
        storage_limit_bytes=1,
        over_bytes=0,
        cleanup_url="https://app.example.test/workspace/settings/plan",
    )


def _lock(module: str, name: str):
    return patch(f"api.routes.{module}.{name}", AsyncMock(side_effect=_locked()))


USER = {"user_id": "u1"}


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------


class TestAgentState:
    @pytest.fixture
    def service(self):
        return MagicMock(
            set_state=AsyncMock(),
            get_state=AsyncMock(),
            list_state=AsyncMock(),
            delete_state=AsyncMock(return_value=True),
        )

    @pytest.fixture
    def perm(self):
        return MagicMock(
            resolve_context_for_workspace_read=AsyncMock(),
            check_context_write=AsyncMock(),
            db=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_set_is_refused_after_the_write_gate(self, service, perm) -> None:
        from api.routes.agent_state import AgentStateSetRequest, set_agent_state

        ctx = uuid4()
        with _lock("agent_state", "ensure_context_not_capacity_locked") as ensure:
            with pytest.raises(CapacityLockedError):
                await set_agent_state(
                    context_id=ctx,
                    user=USER,
                    body=AgentStateSetRequest(value={"a": 1}),
                    key="k",
                    service=service,
                    perm=perm,
                )
        perm.check_context_write.assert_awaited_once()
        service.set_state.assert_not_awaited()
        assert ensure.await_args.args[1] == ctx
        assert ensure.await_args.kwargs == {"user_id": "u1"}

    @pytest.mark.asyncio
    async def test_get_and_list_are_refused(self, service, perm) -> None:
        from api.routes.agent_state import get_agent_state, list_agent_state

        with _lock("agent_state", "ensure_context_not_capacity_locked"):
            with pytest.raises(CapacityLockedError):
                await get_agent_state(
                    context_id=uuid4(), user=USER, key="k", service=service, perm=perm
                )
            with pytest.raises(CapacityLockedError):
                await list_agent_state(context_id=uuid4(), user=USER, service=service, perm=perm)
        service.get_state.assert_not_awaited()
        service.list_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_stays_allowed(self, service, perm) -> None:
        from api.routes.agent_state import delete_agent_state

        with _lock("agent_state", "ensure_context_not_capacity_locked") as ensure:
            await delete_agent_state(
                context_id=uuid4(), user=USER, key="k", service=service, perm=perm
            )
        ensure.assert_not_awaited()
        service.delete_state.assert_awaited_once()


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


class TestFeedback:
    @pytest.mark.asyncio
    async def test_feedback_is_refused_after_the_access_check(self) -> None:
        from api.routes.feedback import FeedbackRequest, record_feedback

        service = MagicMock(record_feedback=AsyncMock())
        perm = MagicMock(check_context_access=AsyncMock(), db=MagicMock())
        with _lock("feedback", "ensure_context_not_capacity_locked"):
            with pytest.raises(CapacityLockedError):
                await record_feedback(
                    context_id=uuid4(),
                    body=FeedbackRequest(memory_id=uuid4(), helpful=True),
                    user=USER,
                    service=service,
                    perm=perm,
                )
        perm.check_context_access.assert_awaited_once()
        service.record_feedback.assert_not_awaited()


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


class TestGraph:
    @pytest.mark.asyncio
    async def test_graph_data_is_refused_after_the_read_gate(self) -> None:
        from api.routes import graph

        ws = uuid4()
        context = SimpleNamespace(id=uuid4(), workspace_id=ws, is_private=True)
        perm = MagicMock(resolve_context_for_workspace_read=AsyncMock(return_value=context))
        with (
            patch.object(graph, "PermissionService", return_value=perm),
            _lock("graph", "ensure_not_capacity_locked") as ensure,
            pytest.raises(CapacityLockedError),
        ):
            await graph.get_graph_data(
                user={"user_id": "u1"},
                db=MagicMock(),
                context_id=context.id,
                limit_nodes=10,
                min_weight=0.0,
                memory_types=None,
            )
        assert ensure.await_args.args[1] == ws
        assert ensure.await_args.kwargs == {"user_id": "u1"}

    @pytest.mark.asyncio
    async def test_graph_stats_is_refused(self) -> None:
        from api.routes import graph

        context = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), is_private=True)
        perm = MagicMock(resolve_context_for_workspace_read=AsyncMock(return_value=context))
        with (
            patch.object(graph, "PermissionService", return_value=perm),
            _lock("graph", "ensure_not_capacity_locked"),
            pytest.raises(CapacityLockedError),
        ):
            await graph.get_graph_stats(
                user={"user_id": "u1"}, context_id=context.id, db=MagicMock()
            )

    @pytest.mark.asyncio
    async def test_creating_an_edge_is_refused(self) -> None:
        from api.routes import graph

        context = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), is_private=True)
        perm = MagicMock(resolve_context_for_workspace_read=AsyncMock(return_value=context))
        body = graph.CreateEdgeRequest(
            context_id=context.id,
            source_id=uuid4(),
            target_id=uuid4(),
            edge_type="related_to",
        )
        db = MagicMock(execute=AsyncMock())
        with (
            patch.object(graph, "PermissionService", return_value=perm),
            _lock("graph", "ensure_not_capacity_locked"),
            pytest.raises(CapacityLockedError),
        ):
            await graph.create_graph_edge(
                body=body, user={"user_id": "u1"}, response=MagicMock(), db=db
            )
        db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------


class TestAnalyses:
    @pytest.mark.asyncio
    async def test_the_context_check_refuses_a_locked_workspace(self) -> None:
        from api.routes import analyses

        ws = uuid4()
        with (
            patch.object(
                analyses.query_service,
                "verify_context_in_workspace",
                AsyncMock(return_value=True),
            ),
            patch(
                "services.agent_binding_service.agent_binding_permits",
                AsyncMock(return_value=True),
            ),
            _lock("analyses", "ensure_not_capacity_locked") as ensure,
        ):
            with pytest.raises(CapacityLockedError):
                await analyses._verify_context_in_workspace(
                    MagicMock(), workspace_id=ws, context_id=uuid4()
                )
            assert ensure.await_args.args[1] == ws
            ensure.reset_mock()
            await analyses._verify_context_in_workspace(
                MagicMock(), workspace_id=ws, context_id=uuid4(), capacity_gate=False
            )
            ensure.assert_not_awaited()

    def test_only_cancel_skips_the_gate(self) -> None:
        """Every analysis route but cancel reads or derives memory content."""
        import ast
        import inspect

        from api.routes import analyses

        tree = ast.parse(inspect.getsource(analyses))
        skipping: set[str] = set()
        gated: set[str] = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in ast.walk(fn):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_verify_context_in_workspace"
                ):
                    off = any(
                        kw.arg == "capacity_gate"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is False
                        for kw in call.keywords
                    )
                    (skipping if off else gated).add(fn.name)
        assert skipping == {"cancel_run"}
        assert {"preview_analysis", "start_analysis", "get_run", "list_runs"} <= gated


# ---------------------------------------------------------------------------
# Sleep reports
# ---------------------------------------------------------------------------


class TestSleepReports:
    @pytest.mark.asyncio
    async def test_workspace_reports_are_refused_after_the_admin_check(self) -> None:
        from api.routes import sleep_reports

        ws = uuid4()
        perm = MagicMock(check_workspace_admin=AsyncMock())
        service_cls = MagicMock()
        with (
            patch.object(sleep_reports, "PermissionService", return_value=perm),
            patch.object(sleep_reports, "_enforce_sleep_plan", AsyncMock()),
            patch.object(sleep_reports, "SleepReporterService", service_cls),
            _lock("sleep_reports", "ensure_not_capacity_locked") as ensure,
            pytest.raises(CapacityLockedError),
        ):
            await sleep_reports.workspace_list_sleep_reports(
                workspace_id=ws,
                user={"user_id": "u1"},
                db=MagicMock(),
                limit=50,
                offset=0,
                status_filter=None,
                context_id=None,
                user_id=None,
            )
        perm.check_workspace_admin.assert_awaited_once()
        service_cls.return_value.list_reports.assert_not_called()
        assert ensure.await_args.args[1] == ws

    @pytest.mark.asyncio
    async def test_a_report_detail_is_refused(self) -> None:
        from api.routes import sleep_reports

        perm = MagicMock(check_workspace_admin=AsyncMock())
        with (
            patch.object(sleep_reports, "PermissionService", return_value=perm),
            patch.object(sleep_reports, "_enforce_sleep_plan", AsyncMock()),
            _lock("sleep_reports", "ensure_not_capacity_locked"),
            pytest.raises(CapacityLockedError),
        ):
            await sleep_reports.workspace_get_sleep_report_detail(
                workspace_id=uuid4(), report_id=uuid4(), user={"user_id": "u1"}, db=MagicMock()
            )


# ---------------------------------------------------------------------------
# Share keys (non-member readers)
# ---------------------------------------------------------------------------


class TestShareKeys:
    PRINCIPAL = {
        "user_id": "key-creator",
        "share_key_context_id": uuid4(),
        "current_workspace_id": uuid4(),
        "share_key_id": "sk",
    }

    @pytest.mark.asyncio
    async def test_share_recall_is_refused_in_the_redacted_form(self) -> None:
        from api.routes.share_keys import share_recall
        from models.schemas import RecallRequest

        memory_service = MagicMock(recall=AsyncMock(), db=MagicMock())
        with _lock("share_keys", "ensure_context_not_capacity_locked") as ensure:
            with pytest.raises(CapacityLockedError):
                await share_recall(
                    request=RecallRequest(query="q"),
                    principal=self.PRINCIPAL,
                    memory_service=memory_service,
                )
        memory_service.recall.assert_not_awaited()
        assert ensure.await_args.args[1] == self.PRINCIPAL["share_key_context_id"]
        assert ensure.await_args.kwargs == {"outsider": True}

    @pytest.mark.asyncio
    async def test_share_sessions_is_refused_in_the_redacted_form(self) -> None:
        from api.routes.share_keys import share_sessions

        service = MagicMock(list_state_detail=AsyncMock(), db=MagicMock())
        with _lock("share_keys", "ensure_context_not_capacity_locked") as ensure:
            with pytest.raises(CapacityLockedError):
                await share_sessions(principal=self.PRINCIPAL, agent_state_service=service)
        service.list_state_detail.assert_not_awaited()
        assert ensure.await_args.kwargs == {"outsider": True}


# ---------------------------------------------------------------------------
# Agent bootstrap
# ---------------------------------------------------------------------------


class TestAgentBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_is_refused_after_context_resolution(self, monkeypatch) -> None:
        from api.routes.agents import BootstrapRequest, agent_bootstrap

        ws = uuid4()
        inst = MagicMock()
        inst.resolve_principal_and_agent = AsyncMock(
            return_value=(
                SimpleNamespace(workspace_id=ws, user_id="u1", metadata={}),
                SimpleNamespace(id=uuid4(), name="bot", workspace_id=ws),
            )
        )
        inst.resolve_context = AsyncMock(
            return_value=(SimpleNamespace(id=uuid4(), workspace_id=ws), {"context_id": "c"})
        )
        inst.build_envelope = AsyncMock()
        monkeypatch.setattr(
            "services.agent_bootstrap_service.AgentBootstrapService",
            MagicMock(return_value=inst),
        )
        db = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
        with _lock("agents", "ensure_not_capacity_locked") as ensure:
            with pytest.raises(CapacityLockedError):
                await agent_bootstrap(
                    agent_id=uuid4(), body=BootstrapRequest(), user={"user_id": "u1"}, db=db
                )
        inst.resolve_context.assert_awaited_once()
        inst.build_envelope.assert_not_awaited()
        assert ensure.await_args.args[1] == ws
        assert ensure.await_args.kwargs == {"user_id": "u1"}


# ---------------------------------------------------------------------------
# Public context search (#1941 review)
# ---------------------------------------------------------------------------


class TestPublicSearch:
    async def _call(self, *, user, bound_key=None, order=None):
        from api.routes import public_search as ps

        ws_id = uuid4()
        ctx = SimpleNamespace(id=uuid4(), workspace_id=ws_id, is_public=True)
        workspace = SimpleNamespace(id=ws_id, plan_name="free")
        order = order if order is not None else []

        async def get(model, _id):
            if model is ps.Context:
                return ctx
            order.append("workspace_loaded")
            return workspace

        db = MagicMock(get=AsyncMock(side_effect=get))
        search = AsyncMock()

        async def anon_bucket(*_a, **_k):
            order.append("anonymous_bucket")

        async def locked(*_a, **_k):
            order.append("capacity")
            raise _locked()

        with (
            patch.object(ps, "_resolve_public_attribution", AsyncMock(return_value=bound_key)),
            patch.object(ps, "check_pre_auth_rate_limit", AsyncMock()),
            patch.object(ps, "check_bound_key_rate_limit", AsyncMock()),
            patch.object(ps, "check_public_search_rate_limit", AsyncMock(side_effect=anon_bucket)),
            patch.object(
                ps, "SearchService", MagicMock(return_value=MagicMock(hybrid_search=search))
            ),
            patch.object(ps, "ensure_not_capacity_locked", AsyncMock(side_effect=locked)) as ensure,
            pytest.raises(CapacityLockedError),
        ):
            await ps.public_search(
                context_id=ctx.id,
                request=ps.PublicSearchRequest(query="q"),
                user=user if user is None else {**user, "current_workspace_id": ws_id},
                api_key="k" if bound_key else None,
                db=db,
            )
        search.assert_not_awaited()
        return ensure, workspace, order

    @pytest.mark.asyncio
    async def test_a_member_session_sees_the_numbers(self) -> None:
        ensure, workspace, _ = await self._call(user={"user_id": "u1"})
        assert ensure.await_args.args[1] is workspace
        assert ensure.await_args.kwargs == {"user_id": "u1"}

    @pytest.mark.asyncio
    async def test_an_anonymous_reader_is_an_outsider_checked_after_its_bucket(self) -> None:
        ensure, workspace, order = await self._call(user=None)
        # The loaded Workspace is passed (no second lookup), after the
        # anonymous bucket and the workspace load.
        assert ensure.await_args.args[1] is workspace
        assert ensure.await_args.kwargs == {"outsider": True}
        assert order == ["anonymous_bucket", "workspace_loaded", "capacity"]

    @pytest.mark.asyncio
    async def test_a_bound_key_reader_is_an_outsider(self) -> None:
        ensure, _, order = await self._call(user=None, bound_key=SimpleNamespace(id=7))
        assert ensure.await_args.kwargs == {"outsider": True}
        assert order == ["workspace_loaded", "capacity"]


# ---------------------------------------------------------------------------
# Context search settings (MCP update_search_config is blocked)
# ---------------------------------------------------------------------------


class TestContextSearchConfig:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["update", "reset"])
    async def test_writes_are_refused_after_the_write_check(self, route: str) -> None:
        from api.routes import context_search_config as csc
        from models.schemas import ContextSearchConfigUpdate

        ctx = uuid4()
        perm = MagicMock(check_context_write=AsyncMock())
        repo = MagicMock(update=AsyncMock(), reset_to_default=AsyncMock())
        with (
            patch.object(csc, "PermissionService", return_value=perm),
            patch.object(csc, "ContextSearchConfigRepository", return_value=repo),
            _lock("context_search_config", "ensure_context_not_capacity_locked") as ensure,
            pytest.raises(CapacityLockedError),
        ):
            if route == "update":
                await csc.update_context_search_config(
                    context_id=ctx,
                    update_data=ContextSearchConfigUpdate.model_construct(fetch_factor=3),
                    user=USER,
                    db=MagicMock(),
                )
            else:
                await csc.reset_context_search_config(context_id=ctx, user=USER, db=MagicMock())
        perm.check_context_write.assert_awaited_once()
        repo.update.assert_not_awaited()
        repo.reset_to_default.assert_not_awaited()
        assert ensure.await_args.args[1] == ctx
        assert ensure.await_args.kwargs == {"user_id": "u1"}

    @pytest.mark.asyncio
    async def test_reading_the_settings_stays_allowed(self) -> None:
        from api.routes import context_search_config as csc

        perm = MagicMock(check_context_write=AsyncMock())
        repo = MagicMock(get_or_create=AsyncMock(side_effect=RuntimeError("reached the read")))
        with (
            patch.object(csc, "PermissionService", return_value=perm),
            patch.object(csc, "ContextSearchConfigRepository", return_value=repo),
            _lock("context_search_config", "ensure_context_not_capacity_locked") as ensure,
        ):
            try:
                await csc.get_context_search_config(context_id=uuid4(), user=USER, db=MagicMock())
            except Exception:  # noqa: BLE001 — the route maps the read failure to 500
                pass
        ensure.assert_not_awaited()
