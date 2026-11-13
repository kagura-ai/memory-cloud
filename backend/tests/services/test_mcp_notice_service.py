"""Service notices over MCP: the documented constants and the pure parts (#2762).

The database behaviour is in tests/integration/test_mcp_notice_service_db.py.
"""

from __future__ import annotations

import pytest

from config.settings import Settings, get_settings
from services import mcp_notice_service as svc
from services import mcp_notice_text as text_rules


class _NoDb:
    """A session stand-in that records every attribute asked of it. A missing
    attribute is an ``AttributeError`` (the protocol ``getattr`` and
    ``hasattr`` expect); the test asserts nothing was asked for."""

    def __init__(self) -> None:
        self.used: list[str] = []

    def __getattr__(self, name: str):
        self.used.append(name)
        raise AttributeError(name)


def test_documented_contract() -> None:
    assert text_rules.MCP_NOTICE_MAX_CHARS == 300
    assert text_rules.MCP_NOTICE_MAX_BYTES == 900
    assert svc.MCP_NOTICE_MAX_ACTIVE == 3
    assert svc.MCP_NOTICE_MAX_DAYS == 30
    assert svc.MCP_NOTICE_MAX_SCHEDULE_DAYS == 90
    assert svc.MCP_NOTICE_MAX_CREATED_PER_DAY == 5
    assert svc.MCP_NOTICE_BUDGET_SHARE == 4
    assert svc.MCP_NOTICES_WORDING_VERSION == "2026-11"


def test_off_by_default() -> None:
    assert Settings(_env_file=None).mcp_notices_enabled is False


def test_the_wording_version_is_offered_only_while_enabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_notices_enabled", False)
    assert svc.mcp_notices_enabled() is False
    assert svc.current_mcp_notice_wording_version() is None
    monkeypatch.setattr(get_settings(), "mcp_notices_enabled", True)
    assert svc.current_mcp_notice_wording_version() == svc.MCP_NOTICES_WORDING_VERSION


def test_the_version_has_the_url_safe_shape() -> None:
    from config.settings import TERMS_VERSION_RE

    assert TERMS_VERSION_RE.fullmatch(svc.MCP_NOTICES_WORDING_VERSION)


@pytest.mark.asyncio
async def test_an_opt_in_without_a_version_is_refused_before_any_query() -> None:
    db = _NoDb()
    with pytest.raises(ValueError):
        await svc.record_mcp_notice_consent(
            db,  # type: ignore[arg-type]
            user_id="u1",
            value=True,
            wording_version=None,
        )
    assert db.used == []


@pytest.mark.asyncio
async def test_claiming_nothing_runs_no_statement() -> None:
    db = _NoDb()
    assert (
        await svc.claim_notice_deliveries(db, user_id="u1", notice_ids=[])  # type: ignore[arg-type]
        == set()
    )
    assert db.used == []


@pytest.mark.asyncio
async def test_the_claim_bounds_its_waits_before_the_insert() -> None:
    """``lock_timeout`` and ``statement_timeout`` are the transaction's first
    two statements, so the database (not a client-side timeout) ends a claim
    stuck behind a lock. The behaviour runs on Postgres in
    tests/integration/test_mcp_notice_service_db.py."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    await svc.claim_notice_deliveries(session, user_id="u1", notice_ids=[uuid.uuid4()])

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert statements[:2] == [
        "SET LOCAL lock_timeout = '1s'",
        "SET LOCAL statement_timeout = '2s'",
    ]
    assert statements[2].lstrip().upper().startswith("INSERT INTO MCP_NOTICE_DELIVERIES")
    assert len(statements) == 3
    session.commit.assert_awaited_once()
