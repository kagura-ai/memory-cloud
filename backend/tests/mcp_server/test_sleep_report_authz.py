"""get_sleep_report / rollback_sleep_run authorize against the report's context.

Owning the report is not enough: both tools also resolve the report's context
the way every other context tool does, and rollback needs the editor or owner
role there — the gate ``rollback_session`` applies. The session's workspace
plays no part. A context the caller cannot reach answers exactly like a
missing report.

The helpers run for real here with the resolvers patched; the real
resolvers, against Postgres, are in
``tests/integration/test_sleep_report_authz_db.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from auth.workspace_roles import ContextRole
from mcp_server.tools._helpers import _ContextNotFoundError
from mcp_server.tools.sleep import (
    _authorize_report_read,
    _authorize_report_rollback,
    _report_not_found,
    handle_get_sleep_report,
    handle_rollback_sleep_run,
)

pytestmark = pytest.mark.asyncio

_USER = "sleep-authz-user"


def _report(context_id=None, *, status="completed"):
    report_id = uuid4()
    return SimpleNamespace(
        id=report_id,
        user_id=_USER,
        context_id=context_id,
        workspace_id=uuid4(),
        status=status,
    )


def _denied(context_id):
    return _ContextNotFoundError(context_id, "Context not found or you don't have access to it.")


# --- _authorize_report_read -------------------------------------------------


async def test_read_authorizes_against_the_reports_context():
    report = _report(uuid4())
    resolve = AsyncMock(return_value=SimpleNamespace(id=report.context_id))
    with patch("mcp_server.tools.sleep._resolve_context_for_read", new=resolve):
        assert await _authorize_report_read(MagicMock(), _USER, report) is True
    resolve.assert_awaited_once()
    assert resolve.await_args_list[0].args[1:] == (_USER, report.context_id)


async def test_read_is_refused_when_the_context_is_out_of_reach():
    report = _report(uuid4())
    resolve = AsyncMock(side_effect=_denied(report.context_id))
    with patch("mcp_server.tools.sleep._resolve_context_for_read", new=resolve):
        assert await _authorize_report_read(MagicMock(), _USER, report) is False


async def test_read_refuses_a_report_without_a_context():
    resolve = AsyncMock()
    with patch("mcp_server.tools.sleep._resolve_context_for_read", new=resolve):
        assert await _authorize_report_read(MagicMock(), _USER, _report(None)) is False
    resolve.assert_not_awaited()


# --- _authorize_report_rollback ---------------------------------------------


@pytest.mark.parametrize("role", [ContextRole.OWNER, ContextRole.EDITOR])
async def test_rollback_is_allowed_for_owner_and_editor(role):
    report = _report(uuid4())
    resolve = AsyncMock(return_value=SimpleNamespace(id=report.context_id))
    with (
        patch("mcp_server.tools.sleep._resolve_context", new=resolve),
        patch("services.session_rollback.context_role", new=AsyncMock(return_value=role)),
    ):
        assert await _authorize_report_rollback(MagicMock(), _USER, report) is None
    resolve.assert_awaited_once()
    assert resolve.await_args_list[0].args[1:] == (_USER, report.context_id)


@pytest.mark.parametrize("role", [ContextRole.VIEWER, None])
async def test_rollback_needs_the_editor_role_on_the_reports_context(role):
    report = _report(uuid4())
    with (
        patch("mcp_server.tools.sleep._resolve_context", new=AsyncMock()),
        patch("services.session_rollback.context_role", new=AsyncMock(return_value=role)),
    ):
        refusal = await _authorize_report_rollback(MagicMock(), _USER, report)
    assert refusal is not None
    data = json.loads(refusal[0].text)
    assert data["error"] == "permission_denied"
    assert data["required_role"] == "editor"
    assert data["your_role"] == (role.value if role is not None else None)


async def test_rollback_on_an_unreachable_context_answers_like_a_missing_report():
    report = _report(uuid4())
    role = AsyncMock()
    with (
        patch(
            "mcp_server.tools.sleep._resolve_context",
            new=AsyncMock(side_effect=_denied(report.context_id)),
        ),
        patch("services.session_rollback.context_role", new=role),
    ):
        refusal = await _authorize_report_rollback(MagicMock(), _USER, report)
    assert refusal is not None
    assert refusal[0].text == _report_not_found(report.id)[0].text
    role.assert_not_awaited()


async def test_rollback_refuses_a_report_without_a_context():
    report = _report(None)
    resolve = AsyncMock()
    with patch("mcp_server.tools.sleep._resolve_context", new=resolve):
        refusal = await _authorize_report_rollback(MagicMock(), _USER, report)
    assert refusal is not None
    assert refusal[0].text == _report_not_found(report.id)[0].text
    resolve.assert_not_awaited()


# --- the handlers call the gate before reading or writing anything ----------


def _db_returning(report):
    result = MagicMock()
    result.scalar_one_or_none.return_value = report
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.rollback = AsyncMock()

    async def get_db():
        yield db

    return db, get_db


async def _missing_report_reply(handler, report_id):
    _db, get_db = _db_returning(None)
    with patch("db.base.get_db", new=get_db):
        return (await handler({"report_id": str(report_id)}, _USER, uuid4()))[0].text


async def test_get_report_refused_by_its_context_is_the_not_found_reply():
    report = _report(uuid4())
    db, get_db = _db_returning(report)
    with (
        patch("db.base.get_db", new=get_db),
        patch("mcp_server.tools.sleep._authorize_report_read", new=AsyncMock(return_value=False)),
    ):
        (block,) = await handle_get_sleep_report({"report_id": str(report.id)}, _USER, uuid4())
    assert block.text == await _missing_report_reply(handle_get_sleep_report, report.id)
    # Only the report lookup ran: no action was read.
    assert db.execute.await_count == 1


async def test_rollback_refused_by_its_context_writes_nothing():
    report = _report(uuid4())
    db, get_db = _db_returning(report)
    refusal = _report_not_found(report.id)
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.sleep._authorize_report_rollback",
            new=AsyncMock(return_value=refusal),
        ),
    ):
        (block,) = await handle_rollback_sleep_run({"report_id": str(report.id)}, _USER, uuid4())
    assert block.text == await _missing_report_reply(handle_rollback_sleep_run, report.id)
    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


async def test_rollback_gate_runs_before_the_status_check():
    """A report the caller may not roll back is refused before its status is
    looked at, so the status of an out-of-reach report is never disclosed."""
    report = _report(uuid4(), status="rolled_back")
    _db, get_db = _db_returning(report)
    with (
        patch("db.base.get_db", new=get_db),
        patch(
            "mcp_server.tools.sleep._authorize_report_rollback",
            new=AsyncMock(return_value=_report_not_found(report.id)),
        ),
    ):
        (block,) = await handle_rollback_sleep_run({"report_id": str(report.id)}, _USER, uuid4())
    assert json.loads(block.text)["error"] == "report_not_found"
