"""``except ValueError`` arms echo only the caller's errors (#1742).

The handlers' ``except ValueError`` arms return ``validation_error`` with
``str(e)`` because the services raise a plain ``ValueError`` as their
bad-request signal. A ``ValueError`` *subclass* raised by server code —
pydantic's ``ValidationError``, ``JSONDecodeError``, ``UnicodeDecodeError`` —
is not the caller's argument: it goes through the #1684 server-failure
envelope (``correlation_id``, and ``outcome: "unknown"`` for writes), and its
text never reaches the caller. The validation subclasses the services raise by
design (``TriggerValidationError``, ``LocationValidationError``,
``ToolTriggerValidationError``) are still echoed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import BaseModel

from mcp_server.tools._errors import is_caller_value_error
from utils.geo_location import LocationValidationError
from utils.time_trigger import TriggerValidationError
from utils.tool_trigger import TOOL_TRIGGER_ERROR_CODES, ToolTriggerValidationError


class _StoredRow(BaseModel):
    importance: float


def _pydantic_error() -> ValueError:
    try:
        _StoredRow(importance="/srv/app/secret/path.py")  # type: ignore[arg-type]
    except ValueError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def _json_error() -> ValueError:
    try:
        json.loads("{/srv/app/config.json")
    except ValueError as exc:
        return exc
    raise AssertionError("expected a JSONDecodeError")


def _unicode_error() -> ValueError:
    return UnicodeDecodeError("utf-8", b"\xff/srv/app/blob", 0, 1, "invalid start byte")


SERVER_ERRORS = [
    pytest.param(_pydantic_error, id="pydantic"),
    pytest.param(_json_error, id="json"),
    pytest.param(_unicode_error, id="unicode"),
]


def _payload(result) -> dict:
    return json.loads(result[0].text)


def _assert_server_failure(payload: dict, *, write: bool) -> None:
    assert payload["status"] == "error"
    assert payload["error"] != "validation_error"
    assert payload["cause"] == "internal_error"
    assert payload["correlation_id"]
    text = json.dumps(payload)
    assert "/srv/app" not in text
    assert "pydantic" not in text.lower()
    if write:
        assert payload["outcome"] == "unknown"
        assert payload["retryable"] is False


class TestIsCallerValueError:
    def test_plain_value_error_and_designed_subclasses_are_the_callers(self):
        assert is_caller_value_error(ValueError("bad"))
        assert is_caller_value_error(TriggerValidationError("bad trigger"))
        assert is_caller_value_error(LocationValidationError("bad location"))
        assert is_caller_value_error(
            ToolTriggerValidationError(sorted(TOOL_TRIGGER_ERROR_CODES)[0], "bad")
        )

    @pytest.mark.parametrize("make", SERVER_ERRORS)
    def test_library_subclasses_are_not(self, make):
        assert not is_caller_value_error(make())

    def test_other_exceptions_are_not(self):
        assert not is_caller_value_error(RuntimeError("x"))


def _mock_db() -> MagicMock:
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    return db


def _memory_patches(db, service):
    async def get_db():
        yield db

    ctx = MagicMock(id=uuid4(), workspace_id=uuid4(), is_private=False)
    return (
        patch("db.base.get_db", new=get_db),
        patch("mcp_server.tools.memory._check_viewer_permission", new=AsyncMock(return_value=None)),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=ctx)),
        patch("mcp_server.tools.memory._resolve_context_for_read", new=AsyncMock(return_value=ctx)),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    )


async def _run(handler, args, db, service):
    p = _memory_patches(db, service)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        return await handler(args, "user-1", uuid4())


REMEMBER_ARGS = {"summary": "a summary long enough", "content": "c", "type": "note"}


class TestRemember:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", SERVER_ERRORS)
    async def test_a_server_value_error_subclass_is_a_write_failure(self, make):
        from mcp_server.tools.memory import handle_remember

        service = MagicMock()
        service.remember = AsyncMock(side_effect=make())
        db = _mock_db()
        result = await _run(
            handle_remember, {"context_id": str(uuid4()), **REMEMBER_ARGS}, db, service
        )

        payload = _payload(result)
        _assert_server_failure(payload, write=True)
        assert "recall" in payload["help"]
        db.rollback.assert_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [ValueError("details.trigger.at must be ISO"), TriggerValidationError("bad trigger")],
    )
    async def test_the_services_own_value_error_is_still_a_validation_error(self, exc):
        from mcp_server.tools.memory import handle_remember

        service = MagicMock()
        service.remember = AsyncMock(side_effect=exc)
        result = await _run(
            handle_remember, {"context_id": str(uuid4()), **REMEMBER_ARGS}, _mock_db(), service
        )

        payload = _payload(result)
        assert payload["error"] == "validation_error"
        assert payload["message"] == str(exc)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("args", "missing"),
        [
            ({"summary": "s", "content": "c"}, ["type"]),
            ({"type": "note"}, ["summary", "content"]),
            ({}, ["summary", "content", "type"]),
        ],
    )
    async def test_missing_fields_names_only_the_missing_ones(self, args, missing):
        from mcp_server.tools.memory import handle_remember

        payload = _payload(await handle_remember({"context_id": str(uuid4()), **args}, "u", None))
        assert payload["error"] == "missing_fields"
        assert payload["missing_fields"] == missing
        assert payload["message"] == f"Missing required fields: {', '.join(missing)}"


class TestUpdateMemory:
    @pytest.mark.asyncio
    async def test_a_server_value_error_subclass_is_a_write_failure(self):
        from mcp_server.tools.memory import handle_update_memory

        service = MagicMock()
        service.update_memory = AsyncMock(side_effect=_json_error())
        result = await _run(
            handle_update_memory,
            {"context_id": str(uuid4()), "memory_id": str(uuid4()), "importance": 0.9},
            _mock_db(),
            service,
        )
        payload = _payload(result)
        _assert_server_failure(payload, write=True)
        assert "reference" in payload["help"]


class TestReads:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", SERVER_ERRORS)
    @pytest.mark.parametrize("tool", ["load_pinned", "load_guardrails"])
    async def test_a_server_value_error_subclass_is_a_read_failure(self, tool, make):
        import mcp_server.tools.memory as memory

        service = MagicMock()
        setattr(service, tool, AsyncMock(side_effect=make()))
        handler = getattr(memory, f"handle_{tool}")
        result = await _run(handler, {"context_id": str(uuid4())}, _mock_db(), service)

        payload = _payload(result)
        _assert_server_failure(payload, write=False)
        assert payload["retryable"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make", SERVER_ERRORS)
    async def test_recall_server_value_error_subclass_is_not_a_validation_error(self, make):
        from mcp_server.tools.memory import handle_recall

        service = MagicMock()
        service.recall = AsyncMock(side_effect=make())
        result = await _run(
            handle_recall, {"context_id": str(uuid4()), "query": "q"}, _mock_db(), service
        )
        payload = _payload(result)
        _assert_server_failure(payload, write=False)

    @pytest.mark.asyncio
    async def test_recall_location_validation_error_is_still_echoed(self):
        from mcp_server.tools.memory import handle_recall

        service = MagicMock()
        service.recall = AsyncMock(side_effect=LocationValidationError("near.lat out of range"))
        result = await _run(
            handle_recall, {"context_id": str(uuid4()), "query": "q"}, _mock_db(), service
        )
        payload = _payload(result)
        assert payload["error"] == "validation_error"
        assert payload["message"] == "near.lat out of range"


class TestSecrets:
    @pytest.mark.asyncio
    async def test_register_pubkey_server_value_error_subclass_is_not_invalid_arguments(self):
        from mcp_server.tools import secrets

        db = _mock_db()

        async def get_db():
            yield db

        service = MagicMock()
        service.register_pubkey = AsyncMock(side_effect=_unicode_error())
        with (
            patch.object(secrets, "get_db", new=get_db),
            patch.object(secrets, "_check_viewer_permission", new=AsyncMock(return_value=None)),
            patch.object(secrets, "_log_tool_usage", new=AsyncMock()),
            patch.object(secrets, "SecretStoreService", new=MagicMock(return_value=service)),
        ):
            result = await secrets.handle_secret_register_pubkey(
                {"pubkey": "age1xyz"}, "user-1", uuid4()
            )
        payload = _payload(result)
        assert payload["error"] == "secret_register_pubkey_error"
        assert payload["correlation_id"]
        assert "/srv/app" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_register_pubkey_plain_value_error_is_still_invalid_arguments(self):
        from mcp_server.tools import secrets

        db = _mock_db()

        async def get_db():
            yield db

        service = MagicMock()
        service.register_pubkey = AsyncMock(side_effect=ValueError("not an age recipient"))
        with (
            patch.object(secrets, "get_db", new=get_db),
            patch.object(secrets, "_check_viewer_permission", new=AsyncMock(return_value=None)),
            patch.object(secrets, "SecretStoreService", new=MagicMock(return_value=service)),
        ):
            result = await secrets.handle_secret_register_pubkey(
                {"pubkey": "age1xyz"}, "user-1", uuid4()
            )
        payload = _payload(result)
        assert payload["error"] == "invalid_arguments"
        assert payload["message"] == "not an age recipient"


class TestMeasurement:
    @pytest.mark.asyncio
    async def test_record_server_value_error_subclass_is_a_write_failure(self):
        from mcp_server.tools import measurement

        db = _mock_db()

        async def get_db():
            yield db

        ctx = MagicMock(workspace_id=uuid4())
        service = MagicMock()
        service.record = AsyncMock(side_effect=_pydantic_error())
        with (
            patch("db.base.get_db", new=get_db),
            patch.object(measurement, "_resolve_context_for_read", new=AsyncMock(return_value=ctx)),
            patch.object(measurement, "_check_viewer_permission", new=AsyncMock(return_value=None)),
            patch(
                "services.agent_binding_service.agent_binding_permits",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "services.measurement_service.MeasurementService",
                new=MagicMock(return_value=service),
            ),
        ):
            result = await measurement.handle_record_measurement(
                {"context_id": str(uuid4()), "metric": "weight", "value": 1.0},
                "user-1",
                uuid4(),
            )
        payload = _payload(result)
        _assert_server_failure(payload, write=True)
        assert "recall_series" in payload["help"]
