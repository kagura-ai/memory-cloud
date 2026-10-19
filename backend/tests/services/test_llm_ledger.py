"""The shared completion ledger helper (#2019 judge recipe, shared for #2144)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import llm_ledger


def _response() -> MagicMock:
    r = MagicMock()
    r.provider, r.model = "openai", "m"
    r.input_tokens, r.output_tokens = 10, 4
    r.cached_input_tokens, r.cache_write_tokens = 0, 0
    return r


def _session_cm():
    session = MagicMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, MagicMock(return_value=session)


async def _record(**overrides):
    kwargs = {
        "caller": "recall",
        "call_metadata": {"stage": "llm_selector"},
        "paid_by": "platform",
        "user_id": "u",
        "workspace_id": "w",
        "context_id": "c",
        "failure_event": "evt_failed",
    }
    kwargs.update(overrides)
    await llm_ledger.record_completion_call(MagicMock(), _response(), **kwargs)


@pytest.mark.asyncio
async def test_writes_one_completion_row_and_commits() -> None:
    session, factory = _session_cm()
    writer = MagicMock()
    writer.return_value.record = AsyncMock(return_value=MagicMock())
    with patch("services.llm_call_log_writer.LLMCallLogWriter", writer):
        await _record(session_factory=factory)
    kwargs = writer.return_value.record.await_args.kwargs
    assert kwargs["caller"] == "recall" and kwargs["call_type"] == "completion"
    assert kwargs["call_metadata"] == {"stage": "llm_selector"}
    assert kwargs["fail_on_error"] is False
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_write_is_logged_never_raised() -> None:
    broken = MagicMock(side_effect=ConnectionError("db down"))
    log = MagicMock()
    await _record(session_factory=broken, log=log)
    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "evt_failed"
    assert log.warning.call_args.kwargs["error_type"] == "ConnectionError"


@pytest.mark.asyncio
async def test_a_writer_that_returned_nothing_is_not_committed() -> None:
    session, factory = _session_cm()
    writer = MagicMock()
    writer.return_value.record = AsyncMock(return_value=None)
    with patch("services.llm_call_log_writer.LLMCallLogWriter", writer):
        await _record(session_factory=factory)
    session.commit.assert_not_called()
