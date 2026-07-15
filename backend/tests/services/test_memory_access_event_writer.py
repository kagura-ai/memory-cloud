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
