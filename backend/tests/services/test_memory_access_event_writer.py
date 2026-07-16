"""Unit tests for the memory_access_events writer (#1278).

Pins the fail-open posture (DB error → structured warning + return None, never
raise; error_type never str(exc)), the validation-raises rule, and the
audited-population gate (emit is a no-op without verified agent identity).
DB access is mocked; the append-only trigger + erasure are integration-tested.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.memory_access_event_writer import (
    emit_memory_access_event,
    record_memory_access_event,
)

WORKSPACE_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()


def _patch_db(stack, *, add_raises=None):
    committed = {"n": 0}
    db = MagicMock()
    db.add = MagicMock(side_effect=add_raises) if add_raises else MagicMock()

    async def _commit():
        committed["n"] += 1

    db.commit = AsyncMock(side_effect=_commit)

    async def gen():
        yield db

    stack.enter_context(patch("db.base.get_db", new=gen))
    return db, committed


class TestRecordValidation:
    @pytest.mark.asyncio
    async def test_bad_operation_raises(self):
        with pytest.raises(ValueError, match="operation"):
            await record_memory_access_event(
                workspace_id=WORKSPACE_ID,
                user_id="u",
                principal_type="api_key",
                operation="bogus",
                outcome="success",
                surface="mcp",
            )

    @pytest.mark.asyncio
    async def test_bad_outcome_raises(self):
        with pytest.raises(ValueError, match="outcome"):
            await record_memory_access_event(
                workspace_id=WORKSPACE_ID,
                user_id="u",
                principal_type="api_key",
                operation="recall",
                outcome="ok",
                surface="mcp",
            )

    @pytest.mark.asyncio
    async def test_bad_policy_decision_raises(self):
        with pytest.raises(ValueError, match="policy_decision"):
            await record_memory_access_event(
                workspace_id=WORKSPACE_ID,
                user_id="u",
                principal_type="api_key",
                operation="recall",
                outcome="denied",
                surface="mcp",
                policy_decision="nope",
            )


class TestRecordFailOpen:
    @pytest.mark.asyncio
    async def test_success_inserts_and_commits(self):
        from contextlib import ExitStack

        with ExitStack() as stack:
            db, committed = _patch_db(stack)
            await record_memory_access_event(
                workspace_id=WORKSPACE_ID,
                user_id="u",
                principal_type="api_key",
                operation="bootstrap",
                outcome="success",
                surface="rest",
                agent_id=AGENT_ID,
            )
        db.add.assert_called_once()
        assert committed["n"] == 1

    @pytest.mark.asyncio
    async def test_db_error_is_swallowed(self):
        from contextlib import ExitStack

        with ExitStack() as stack:
            _patch_db(stack, add_raises=RuntimeError("db down"))
            warn = MagicMock()
            stack.enter_context(
                patch("services.memory_access_event_writer.logger.warning", new=warn)
            )
            # Must NOT raise (fail-open).
            await record_memory_access_event(
                workspace_id=WORKSPACE_ID,
                user_id="u",
                principal_type="api_key",
                operation="recall",
                outcome="success",
                surface="mcp",
            )
        warn.assert_called_once()
        kwargs = warn.call_args.kwargs
        # error_type only — never str(exc) (credential-leak guard).
        assert kwargs["error_type"] == "RuntimeError"
        assert "db down" not in str(kwargs)


class TestEmitGate:
    @pytest.mark.asyncio
    async def test_no_agent_scope_is_noop(self):
        recorder = AsyncMock()
        with (
            patch("auth.agent_scope.get_agent_scope", return_value=None),
            patch("services.memory_access_event_writer.record_memory_access_event", new=recorder),
        ):
            await emit_memory_access_event(
                operation="load_pinned",
                outcome="success",
                workspace_id=WORKSPACE_ID,
                user_id="u",
            )
        recorder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_scope_emits_with_correlation(self):
        recorder = AsyncMock()
        scope = SimpleNamespace(agent_id=AGENT_ID, enforcement_mode="enforce")
        corr = SimpleNamespace(
            session_id="s1", run_id="r1", trace_id="t", span_id="sp", surface="mcp"
        )
        with (
            patch("auth.agent_scope.get_agent_scope", return_value=scope),
            patch("api.correlation.get_correlation", return_value=corr),
            patch("services.memory_access_event_writer.record_memory_access_event", new=recorder),
        ):
            await emit_memory_access_event(
                operation="load_pinned",
                outcome="success",
                workspace_id=WORKSPACE_ID,
                user_id="u",
                result_count=3,
            )
        recorder.assert_awaited_once()
        kwargs = recorder.await_args.kwargs
        assert kwargs["agent_id"] == AGENT_ID
        assert kwargs["session_id"] == "s1"
        assert kwargs["surface"] == "mcp"
        assert kwargs["result_count"] == 3

    @pytest.mark.asyncio
    async def test_query_is_hashed_never_stored_raw(self):
        # #1281 item 7: emit hashes the raw query with the audit key; the row
        # writer only ever receives query_hash, never the raw text.
        recorder = AsyncMock()
        scope = SimpleNamespace(agent_id=AGENT_ID, enforcement_mode="enforce")
        with (
            patch("auth.agent_scope.get_agent_scope", return_value=scope),
            patch("api.correlation.get_correlation", return_value=None),
            patch("services.memory_access_event_writer.record_memory_access_event", new=recorder),
        ):
            await emit_memory_access_event(
                operation="recall",
                outcome="success",
                workspace_id=WORKSPACE_ID,
                user_id="u",
                query="secret query text",
            )
        kwargs = recorder.await_args.kwargs
        assert "query" not in kwargs  # raw query never forwarded to the row writer
        assert kwargs["query_hash"] and kwargs["query_hash"] != "secret query text"
        assert len(kwargs["query_hash"]) == 64  # HMAC-SHA256 hex

    @pytest.mark.asyncio
    async def test_missing_workspace_is_noop(self):
        recorder = AsyncMock()
        scope = SimpleNamespace(agent_id=AGENT_ID, enforcement_mode="enforce")
        with (
            patch("auth.agent_scope.get_agent_scope", return_value=scope),
            patch("services.memory_access_event_writer.record_memory_access_event", new=recorder),
        ):
            await emit_memory_access_event(
                operation="recall", outcome="success", workspace_id=None, user_id="u"
            )
        recorder.assert_not_awaited()


class TestWouldDenyDedup:
    """#1286 (P0-5): request-scoped dedup for shadow would_deny rows.

    Several binding gates can evaluate the SAME denied context in one request
    (MCP pre-gate → declared-context isolation gate → memory-id gate); the
    writer collapses those to one row, keyed on
    (operation, requested_context_id, access). Distinct keys still land their
    own rows, and hard denies are never deduped. Task-context isolation gives
    every request (and every test) a fresh set.
    """

    @staticmethod
    def _scope():
        return SimpleNamespace(agent_id=AGENT_ID, workspace_id=WORKSPACE_ID)

    async def _emit(
        self, recorder, *, policy, ctx, access="write", operation="forget", filter_kind=None
    ):
        metadata = {"requested_context_id": ctx, "access": access}
        if filter_kind is not None:
            metadata["filter_kind"] = filter_kind
        with (
            patch("auth.agent_scope.get_agent_scope", return_value=self._scope()),
            patch("api.correlation.get_correlation", return_value=None),
            patch("services.memory_access_event_writer.record_memory_access_event", new=recorder),
        ):
            await emit_memory_access_event(
                operation=operation,
                outcome="success" if policy == "would_deny" else "denied",
                workspace_id=WORKSPACE_ID,
                user_id="u",
                policy_decision=policy,
                extra_metadata=metadata,
            )

    @pytest.mark.asyncio
    async def test_same_key_second_would_deny_skipped(self):
        recorder = AsyncMock()
        ctx = str(uuid.uuid4())
        await self._emit(recorder, policy="would_deny", ctx=ctx)
        await self._emit(recorder, policy="would_deny", ctx=ctx)
        assert recorder.await_count == 1

    @pytest.mark.asyncio
    async def test_distinct_context_lands_own_row(self):
        # update's declared context vs the memory's own context are DISTINCT
        # shadow signals — both must persist.
        recorder = AsyncMock()
        await self._emit(recorder, policy="would_deny", ctx=str(uuid.uuid4()))
        await self._emit(recorder, policy="would_deny", ctx=str(uuid.uuid4()))
        assert recorder.await_count == 2

    @pytest.mark.asyncio
    async def test_distinct_operation_lands_own_row(self):
        recorder = AsyncMock()
        ctx = str(uuid.uuid4())
        await self._emit(recorder, policy="would_deny", ctx=ctx, operation="remember")
        await self._emit(recorder, policy="would_deny", ctx=ctx, operation="forget")
        assert recorder.await_count == 2

    @pytest.mark.asyncio
    async def test_distinct_filter_kind_lands_own_row(self):
        # #1299: the row-level type/source filter emits its shadow aggregate
        # with filter_kind='type_source'. The context-level gate's would_deny
        # for the SAME (operation, context, access) is a DIFFERENT signal —
        # both must persist, so filter_kind participates in the dedup key.
        recorder = AsyncMock()
        ctx = str(uuid.uuid4())
        await self._emit(recorder, policy="would_deny", ctx=ctx, access="read")
        await self._emit(
            recorder, policy="would_deny", ctx=ctx, access="read", filter_kind="type_source"
        )
        assert recorder.await_count == 2

    @pytest.mark.asyncio
    async def test_same_filter_kind_key_deduped(self):
        recorder = AsyncMock()
        ctx = str(uuid.uuid4())
        await self._emit(
            recorder, policy="would_deny", ctx=ctx, access="read", filter_kind="type_source"
        )
        await self._emit(
            recorder, policy="would_deny", ctx=ctx, access="read", filter_kind="type_source"
        )
        assert recorder.await_count == 1

    @pytest.mark.asyncio
    async def test_hard_denies_never_deduped(self):
        recorder = AsyncMock()
        ctx = str(uuid.uuid4())
        await self._emit(recorder, policy="binding_denied", ctx=ctx)
        await self._emit(recorder, policy="binding_denied", ctx=ctx)
        assert recorder.await_count == 2

    @pytest.mark.asyncio
    async def test_fresh_task_context_resets(self):
        # Each request runs in its own task context — the dedup set never
        # leaks across requests (same isolation guarantee AgentScope uses).
        import asyncio

        recorder = AsyncMock()
        ctx = str(uuid.uuid4())

        async def _one_request():
            await self._emit(recorder, policy="would_deny", ctx=ctx)

        await asyncio.create_task(_one_request())
        await asyncio.create_task(_one_request())
        assert recorder.await_count == 2
