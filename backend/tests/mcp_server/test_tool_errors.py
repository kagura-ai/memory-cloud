"""The #1684 tool-error vocabulary (``mcp_server.tools._errors``).

Pins the helper itself — read-only classification, cause classification,
correlation ids, refusal vs server-failure envelopes — and one handler path
that keeps its legacy per-tool code. The dispatch catch-alls are covered in
``test_error_envelopes.py`` and the transport fallbacks in
``test_transport_streamable_post.py`` / ``test_transport_stateless.py``.

Retry semantics are only asserted on unambiguous tools (``list_contexts``
reads; ``forget`` / ``remember`` / ``secret_put`` write): tool annotations may
reclassify borderline tools such as ``recall``.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from mcp_server.tools import _build_registry
from mcp_server.tools._errors import (
    _VERIFY_WITH,
    CAUSE_INTERNAL_ERROR,
    CAUSE_SERVICE_UNAVAILABLE,
    CAUSE_TIMEOUT,
    _tool_exception_response,
    classify_cause,
    describe_tool_exception,
    is_read_only_tool,
    new_correlation_id,
    read_only_hint,
)
from mcp_server.tools._helpers import ToolErrorContent
from utils.exceptions import (
    DatabaseConnectionError,
    ExternalServiceError,
    ValidationError,
)

_LEAKY = "connection to postgresql://svc:hunter2@10.0.0.5/kagura failed; see /srv/app/db.py"


def _payload(result) -> dict:
    assert isinstance(result, ToolErrorContent)
    return json.loads(result[0].text)


# ============================================================================
# Read-only classification
# ============================================================================


class TestReadOnlyHint:
    @pytest.mark.parametrize(
        ("definition", "expected"),
        [
            ({"name": "t", "annotations": {"readOnlyHint": True}}, True),
            # The standard hint wins over the legacy flag, in both directions.
            ({"name": "t", "readOnly": True, "annotations": {"readOnlyHint": False}}, False),
            ({"name": "t", "annotations": {"readOnlyHint": True}, "readOnly": False}, True),
            # No standard hint: the legacy flag decides.
            ({"name": "t", "readOnly": True}, True),
            ({"name": "t", "readOnly": True, "annotations": {"title": "T"}}, True),
            # No hint at all counts as a write.
            ({"name": "t"}, False),
            ({"name": "t", "annotations": None}, False),
        ],
    )
    def test_prefers_the_standard_annotation_over_the_legacy_flag(self, definition, expected):
        assert read_only_hint(definition) is expected

    def test_registry_reads_and_writes(self):
        assert is_read_only_tool("list_contexts") is True
        assert is_read_only_tool("forget") is False
        assert is_read_only_tool("remember") is False

    @pytest.mark.parametrize("name", ["no_such_tool", None, 5, ["list_contexts"]])
    def test_unknown_or_malformed_names_count_as_writes(self, name):
        assert is_read_only_tool(name) is False

    def test_verify_hints_name_registered_tools(self):
        registry = _build_registry()
        for write_tool, read_tool in _VERIFY_WITH.items():
            assert write_tool in registry, write_tool
            assert read_tool in registry, read_tool


# ============================================================================
# Cause classification
# ============================================================================


class TestClassifyCause:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError(),
            httpx.ReadTimeout("read timed out"),
        ],
    )
    def test_timeouts(self, exc):
        assert classify_cause(exc) == CAUSE_TIMEOUT

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionRefusedError(111, "refused"),
            OperationalError("SELECT 1", {}, Exception("server closed the connection")),
            DatabaseConnectionError(),
            ExternalServiceError("Qdrant", "unreachable"),
            httpx.ConnectError("connect failed"),
        ],
    )
    def test_dependency_failures(self, exc):
        assert classify_cause(exc) == CAUSE_SERVICE_UNAVAILABLE

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("boom"),
            KeyError("missing"),
            IntegrityError("INSERT", {}, Exception("duplicate key")),
        ],
    )
    def test_everything_else_is_internal(self, exc):
        assert classify_cause(exc) == CAUSE_INTERNAL_ERROR


# ============================================================================
# Correlation id
# ============================================================================


class TestCorrelationId:
    def test_reuses_the_request_trace_id(self):
        from api.correlation import build_correlation_from_headers, set_correlation

        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        set_correlation(
            build_correlation_from_headers(
                traceparent=f"00-{trace_id}-00f067aa0ba902b7-01", baggage=None, surface="mcp"
            )
        )
        try:
            assert new_correlation_id() == trace_id
        finally:
            set_correlation(None)

    def test_falls_back_to_a_random_id(self):
        first, second = new_correlation_id(), new_correlation_id()
        assert re.fullmatch(r"[0-9a-f]{16}", first)
        assert first != second


# ============================================================================
# Envelopes
# ============================================================================


class TestDescribeToolException:
    def test_server_failure_logs_the_exception_and_returns_none_of_it(self):
        exc = RuntimeError(_LEAKY)
        with patch("mcp_server.tools._errors.logger") as log:
            failure = describe_tool_exception("list_contexts", exc)

        rendered = json.dumps({"message": failure.message, **failure.jsonrpc_data()})
        for fragment in ("hunter2", "postgresql://", "/srv/app", "RuntimeError"):
            assert fragment not in rendered
        log.error.assert_called_once()
        call = log.error.call_args
        assert call.kwargs["exc_info"] is exc
        assert exc in call.args
        assert failure.fields["correlation_id"] in call.args

    def test_legacy_code_is_kept_and_cause_carries_the_category(self):
        failure = describe_tool_exception("secret_put", TimeoutError(), error="secret_put_error")

        assert failure.error == "secret_put_error"
        assert failure.fields["cause"] == CAUSE_TIMEOUT
        assert failure.fields["retryable"] is False
        assert failure.fields["outcome"] == "unknown"
        assert "secret_list" in failure.fields["help"]

    def test_legacy_code_keeps_a_designed_refusal_message(self):
        """``merge_contexts_error`` has always carried the service's validation
        message; that contract is unchanged (only a help line is added)."""
        exc = ValidationError("Source and target contexts must be different.")
        payload = _payload(
            _tool_exception_response("merge_contexts", exc, error="merge_contexts_error")
        )

        assert payload["error"] == "merge_contexts_error"
        assert payload["message"] == "Source and target contexts must be different."
        assert payload["help"]
        assert "correlation_id" not in payload

    def test_permission_error_text_is_never_echoed(self):
        """``PermissionError`` is an ``OSError``: its text can name a server path."""
        exc = PermissionError(13, "Permission denied", "/srv/app/keys/private.pem")
        payload = _payload(_tool_exception_response("list_contexts", exc))

        assert payload["error"] == "permission_denied"
        assert "/srv/app" not in json.dumps(payload)

    def test_refusals_log_a_warning_without_a_traceback(self):
        with patch("mcp_server.tools._errors.logger") as log:
            describe_tool_exception("forget", ValueError("Provide memory_id or query."))

        log.warning.assert_called_once()
        log.error.assert_not_called()

    def test_unknown_tool_name_gets_a_generic_subject_and_write_advice(self):
        failure = describe_tool_exception(["not", "a", "name"], RuntimeError("x"))

        assert failure.message == "The tool call failed because of an unexpected server error."
        assert failure.fields["retryable"] is False


# ============================================================================
# A handler path: secret_put keeps ``secret_put_error``
# ============================================================================


def _secret_put_args() -> dict:
    return {
        "name": "deploy-key",
        "ciphertext": "-----BEGIN AGE ENCRYPTED FILE-----",
        "recipients_snapshot": ["age1fingerprint"],
        "grant_pubkey_ids": [str(uuid4())],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (RuntimeError(_LEAKY), CAUSE_INTERNAL_ERROR),
        (
            OperationalError("INSERT INTO secrets", {}, ConnectionResetError(_LEAKY)),
            CAUSE_SERVICE_UNAVAILABLE,
        ),
    ],
)
async def test_secret_put_failure_is_actionable_and_safe(exc, cause):
    from mcp_server.tools.secrets import handle_secret_put

    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()

    async def fake_get_db():
        yield db

    service = MagicMock()
    service.put_secret = AsyncMock(side_effect=exc)
    with (
        patch("mcp_server.tools.secrets.get_db", new=fake_get_db),
        patch(
            "mcp_server.tools.secrets._get_workspace_member_role",
            new=AsyncMock(return_value="owner"),
        ),
        patch("mcp_server.tools.secrets.SecretStoreService", return_value=service),
        patch("mcp_server.tools.secrets._log_tool_usage", new=AsyncMock()) as log_usage,
    ):
        result = await handle_secret_put(_secret_put_args(), "user-1", uuid4())

    payload = _payload(result)
    assert payload["error"] == "secret_put_error"  # legacy code kept
    assert payload["cause"] == cause
    assert payload["retryable"] is False
    assert payload["outcome"] == "unknown"
    assert "secret_list" in payload["help"]
    assert payload["correlation_id"]
    text = result[0].text
    for fragment in ("hunter2", "postgresql://", "/srv/app", "INSERT"):
        assert fragment not in text
    db.rollback.assert_awaited_once()
    assert log_usage.await_args.args[4] == 500


# ============================================================================
# Context resolution no longer relabels a database failure as context_not_found
# ============================================================================


@pytest.mark.asyncio
async def test_resolve_context_lets_a_database_failure_propagate():
    from mcp_server.tools._helpers import _resolve_context

    service = MagicMock()
    service.get_context = AsyncMock(
        side_effect=OperationalError("SELECT", {}, ConnectionRefusedError(_LEAKY))
    )
    with (
        patch("services.context_service.ContextService", return_value=service),
        pytest.raises(OperationalError),
    ):
        await _resolve_context(MagicMock(), "user-1", uuid4())


@pytest.mark.asyncio
async def test_resolve_context_keeps_the_uniform_not_found_denial():
    from mcp_server.tools._helpers import _ContextNotFoundError, _resolve_context
    from utils.exceptions import NotFoundException

    service = MagicMock()
    service.get_context = AsyncMock(side_effect=NotFoundException("Context", "c-1"))
    with (
        patch("services.context_service.ContextService", return_value=service),
        pytest.raises(_ContextNotFoundError) as caught,
    ):
        await _resolve_context(MagicMock(), "user-1", uuid4())

    assert caught.value.message == "Context not found or you don't have access to it."
