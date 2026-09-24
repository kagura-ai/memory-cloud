"""The #1684 tool-error vocabulary (``mcp_server.tools._errors``).

Pins the helper itself — read-only classification, cause classification,
correlation ids, refusal vs server-failure envelopes — and one handler path
that keeps its legacy per-tool code. The dispatch catch-alls are covered in
``test_error_envelopes.py`` and the transport fallbacks in
``test_transport_streamable_post.py`` / ``test_transport_stateless.py``.

Retry semantics are only asserted on unambiguous tools (``list_contexts``
reads; ``forget`` / ``remember`` / ``secret_put`` write): tool annotations may
reclassify borderline tools such as ``recall``. The repeat-safe writes are
tested with that reclassification simulated, so the advice holds either way.
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
    _REPEAT_SAFE_WRITES,
    _UNVERIFIABLE_WRITES,
    _VERIFY_WITH,
    CAUSE_INTERNAL_ERROR,
    CAUSE_SERVICE_UNAVAILABLE,
    CAUSE_TIMEOUT,
    _is_repeat_safe,
    _tool_exception_response,
    _tool_hints,
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

    def test_every_write_has_specific_retry_advice(self):
        """A tool that is not read-only needs a verify read, a repeat-safe
        reason or an unverifiable-write sentence — otherwise a reclassification
        (#1683 annotations) would silently fall back to the generic "check the
        current state", which a model cannot act on."""
        advised = _VERIFY_WITH.keys() | _REPEAT_SAFE_WRITES.keys() | _UNVERIFIABLE_WRITES.keys()
        uncovered = sorted(
            name
            for name, read_only in _tool_hints().items()
            if not read_only and name not in advised
        )
        assert uncovered == []
        for name in advised:
            assert name in _tool_hints(), name

    def test_verify_reads_are_safe_to_call(self):
        """The read a failed write points at must itself be safe to repeat."""
        for write_tool, read_tool in _VERIFY_WITH.items():
            assert _is_repeat_safe(read_tool), (write_tool, read_tool)

    @pytest.mark.parametrize("name", ["secret_get", "secret_list"])
    def test_secret_reads_are_read_only(self, name):
        """``secret_list`` is the verify read for ``secret_put``; ``secret_get``
        writes only its fetch audit."""
        assert is_read_only_tool(name) is True


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
        event = log.error.call_args
        assert event.args == ("mcp_tool_failed",)
        assert event.kwargs["exc_info"] is exc
        assert event.kwargs["exc"] == _LEAKY
        assert event.kwargs["correlation_id"] == failure.fields["correlation_id"]
        assert event.kwargs["cause"] == CAUSE_INTERNAL_ERROR

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

    def test_designed_refusals_log_a_warning_without_a_traceback(self):
        with patch("mcp_server.tools._errors.logger") as log:
            describe_tool_exception("forget", ValidationError("limit must be positive"))

        log.warning.assert_called_once()
        assert "exc_info" not in log.warning.call_args.kwargs
        log.error.assert_not_called()

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("invalid literal for int() with base 10: 'abc'"),
            PermissionError(13, "Permission denied", "/srv/app/keys/private.pem"),
        ],
    )
    def test_builtin_refusals_keep_their_text_and_traceback_in_the_log(self, exc):
        """A builtin ValueError / PermissionError can be a server bug: the log
        keeps what the caller does not get (the path) or only partly gets."""
        with patch("mcp_server.tools._errors.logger") as log:
            describe_tool_exception("forget", exc)

        event = log.warning.call_args
        assert event.args == ("mcp_tool_refused",)
        assert event.kwargs["exc_info"] is exc
        assert event.kwargs["exc"] == str(exc)

    def test_value_error_is_a_server_failure_where_the_message_was_always_fixed(self):
        """``echo_value_error=False`` (analysis, api_keys, agent_bootstrap,
        secrets): the #1247 fixed-message guarantee covers a plain ValueError."""
        exc = ValueError('relation "memory_analyses" password=SUPERSECRET host=10.0.0.5')
        with patch("mcp_server.tools._errors.logger") as log:
            failure = describe_tool_exception(
                "get_analysis", exc, error="get_analysis_error", echo_value_error=False
            )

        rendered = json.dumps({"message": failure.message, **failure.jsonrpc_data()})
        assert "SUPERSECRET" not in rendered and "10.0.0.5" not in rendered
        assert failure.error == "get_analysis_error"
        assert failure.fields["cause"] == CAUSE_INTERNAL_ERROR
        assert failure.fields["correlation_id"]
        event = log.error.call_args
        assert event.kwargs["exc_info"] is exc
        assert "SUPERSECRET" in event.kwargs["exc"]

    def test_unknown_tool_name_gets_a_generic_subject_and_write_advice(self):
        failure = describe_tool_exception(["not", "a", "name"], RuntimeError("x"))

        assert failure.message == "The tool call failed because of an unexpected server error."
        assert failure.fields["retryable"] is False


# ============================================================================
# Writes with their own retry advice
# ============================================================================


def _as_after_1683():
    """#1683 marks ``recall`` / ``get_agent_bootstrap`` not read-only (their
    ranking-learning writes). Simulate that, whatever this branch says."""
    hints = {**_tool_hints(), "recall": False, "get_agent_bootstrap": False}
    return patch("mcp_server.tools._errors._tool_hints", return_value=hints)


class TestRepeatSafeWrites:
    @pytest.mark.parametrize("tool", ["recall", "get_agent_bootstrap"])
    def test_a_search_timeout_is_retryable_even_when_it_counts_as_a_write(self, tool):
        with _as_after_1683():
            failure = describe_tool_exception(tool, TimeoutError())

        assert failure.fields["retryable"] is True
        assert failure.fields["retry_after_seconds"] == 5
        assert "outcome" not in failure.fields
        help_text = failure.fields["help"]
        assert "ranking updates" in help_text
        assert "calling it again is safe" in help_text
        assert "applied twice" not in help_text

    def test_an_internal_error_allows_one_more_attempt(self):
        with _as_after_1683():
            failure = describe_tool_exception("recall", RuntimeError("x"))

        assert failure.fields["retryable"] is True
        assert "retry_after_seconds" not in failure.fields
        assert "one more attempt is safe" in failure.fields["help"]

    def test_pubkey_registration_is_safe_to_repeat(self):
        failure = describe_tool_exception("secret_register_pubkey", TimeoutError())

        assert failure.fields["retryable"] is True
        assert "duplicate" in failure.fields["help"]

    def test_feedback_says_what_a_repeat_does(self):
        failure = describe_tool_exception("feedback", TimeoutError())

        assert failure.fields["retryable"] is False
        assert failure.fields["outcome"] == "unknown"
        assert "append-only" in failure.fields["help"]
        assert "Check the current state" not in failure.fields["help"]

    def test_setup_connector_names_its_verify_read(self):
        failure = describe_tool_exception("setup_connector", TimeoutError())

        assert "list_resource_tokens" in failure.fields["help"]


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


# ============================================================================
# Handler paths that used to label or echo a dependency failure
# ============================================================================


@pytest.mark.asyncio
async def test_update_search_config_reports_a_database_failure_as_one():
    """Only the designed denials are ``permission_denied``: a database failure
    in the permission check reaches the catch-all with the vocabulary."""
    from mcp_server.tools.search_config import handle_update_search_config

    db = MagicMock()
    db.rollback = AsyncMock()

    async def fake_get_db():
        yield db

    permissions = MagicMock()
    permissions.check_context_write = AsyncMock(
        side_effect=OperationalError("SELECT", {}, ConnectionRefusedError(_LEAKY))
    )
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch("services.permission_service.PermissionService", return_value=permissions),
    ):
        result = await handle_update_search_config(
            {"context_id": str(uuid4()), "rerank_enabled": True}, "user-1", uuid4()
        )

    payload = _payload(result)
    assert payload["error"] == "update_search_config_error"
    assert payload["cause"] == CAUSE_SERVICE_UNAVAILABLE
    assert payload["outcome"] == "unknown"
    assert "get_context_info" in payload["help"]
    for fragment in ("hunter2", "postgresql://", "/srv/app", "SELECT"):
        assert fragment not in result[0].text
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_search_config_keeps_the_designed_denial():
    from mcp_server.tools.search_config import handle_update_search_config
    from utils.exceptions import AuthorizationError

    async def fake_get_db():
        yield MagicMock()

    permissions = MagicMock()
    permissions.check_context_write = AsyncMock(side_effect=AuthorizationError())
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch("services.permission_service.PermissionService", return_value=permissions),
    ):
        result = await handle_update_search_config(
            {"context_id": str(uuid4()), "rerank_enabled": True}, "user-1", uuid4()
        )

    assert _payload(result)["error"] == "permission_denied"


# The storage layer's own messages: an object key, and operator configuration.
_STORAGE_TEXTS = [
    "head_object failed for key='ws-1/ab/abcdef': AccessDenied",
    "storage is not configured (STORAGE_ENDPOINT_URL empty). Set STORAGE_ACCESS_KEY_ID",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_text", _STORAGE_TEXTS)
@pytest.mark.parametrize(
    ("tool", "method", "args", "read_only"),
    [
        (
            "init_file_upload",
            "reserve_upload",
            {"filename": "a.txt", "content_type": "text/plain", "size_bytes": 3, "sha256": "ab"},
            False,
        ),
        (
            "complete_file_upload",
            "confirm_upload",
            {"file_id": str(uuid4()), "sha256": "ab"},
            False,
        ),
        ("get_file_download_url", "get_presigned_download", {"file_id": str(uuid4())}, True),
    ],
)
async def test_file_storage_failures_use_the_vocabulary(
    tool, method, args, read_only, storage_text
):
    """A storage failure (``ExternalServiceError``) used to come back as
    ``service_unavailable`` carrying the storage text and no retry advice."""
    from mcp_server.tools import execute_tool_call
    from services.file_storage_service import FileStorageService

    async def fake_get_db():
        yield MagicMock()

    exc = ExternalServiceError("storage", storage_text)
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch("mcp_server.tools.files._check_viewer_permission", new=AsyncMock(return_value=None)),
        patch(
            "mcp_server.tools.files._check_workspace_membership", new=AsyncMock(return_value=None)
        ),
        patch.object(FileStorageService, method, new=AsyncMock(side_effect=exc)),
    ):
        result = await execute_tool_call(tool, {**args, "workspace_id": str(uuid4())}, "user-1")

    payload = _payload(result)
    assert payload["error"] == CAUSE_SERVICE_UNAVAILABLE  # the code clients already match
    assert payload["cause"] == CAUSE_SERVICE_UNAVAILABLE
    assert payload["correlation_id"]
    assert payload["retryable"] is read_only
    if read_only:
        assert payload["retry_after_seconds"] == 5
    else:
        assert payload["outcome"] == "unknown"
        assert "list_files" in payload["help"]
    for fragment in ("head_object", "ws-1/ab", "STORAGE_", "storage service error"):
        assert fragment not in result[0].text


def test_context_instructions_tell_the_model_how_to_act_on_an_error():
    """The instructions ``get_context_info`` / ``get_agent_bootstrap`` return
    are where a calling model learns the envelope's fields (#1684)."""
    from mcp_server.tools._constants import KAGURA_MEMORY_INSTRUCTIONS

    section = KAGURA_MEMORY_INSTRUCTIONS.split("## Errors", 1)[1].split("##", 1)[0]
    for field in ("error", "help", "retryable", "retry_after_seconds", "outcome", "correlation_id"):
        assert field in section, field
