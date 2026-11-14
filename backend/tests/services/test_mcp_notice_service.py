"""Service notices over MCP: the documented constants and the pure parts (#2762).

The database behaviour is in tests/integration/test_mcp_notice_service_db.py.
#2800: the plan list's normalisation, the plan predicate's place in the one
claim statement, and the create lock key the f44 downgrade shares.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
        await svc.claim_notice_deliveries(
            db,  # type: ignore[arg-type]
            user_id="u1",
            notice_ids=[],
            workspace_id=None,
        )
        == set()
    )
    assert db.used == []


async def _claim_statements(workspace_id: uuid.UUID | None) -> list[str]:
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    await svc.claim_notice_deliveries(
        session, user_id="u1", notice_ids=[uuid.uuid4()], workspace_id=workspace_id
    )

    session.commit.assert_awaited_once()
    return [str(call.args[0]) for call in session.execute.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", [uuid.uuid4(), None], ids=["workspace", "none"])
async def test_the_claim_bounds_its_waits_before_the_insert(workspace_id) -> None:
    """``lock_timeout`` and ``statement_timeout`` are the transaction's first
    two statements, so the database (not a client-side timeout) ends a claim
    stuck behind a lock. The behaviour runs on Postgres in
    tests/integration/test_mcp_notice_service_db.py. #2800: the plan check
    adds no statement (no separate plan read, no lock on the workspace)."""
    statements = await _claim_statements(workspace_id)

    assert statements[:2] == [
        "SET LOCAL lock_timeout = '1s'",
        "SET LOCAL statement_timeout = '2s'",
    ]
    assert statements[2].lstrip().upper().startswith("INSERT INTO MCP_NOTICE_DELIVERIES")
    assert len(statements) == 3


@pytest.mark.asyncio
async def test_the_claim_reads_the_plan_inside_the_insert() -> None:
    """With a workspace id the one INSERT reads the workspace's plan."""
    insert = (await _claim_statements(uuid.uuid4()))[-1]
    assert "workspaces" in insert
    assert "= ANY" in insert
    assert "FOR SHARE" not in insert.upper() and "FOR UPDATE" not in insert.upper()


@pytest.mark.asyncio
async def test_a_claim_without_a_workspace_takes_only_untargeted_notices() -> None:
    insert = (await _claim_statements(None))[-1]
    assert "mcp_notices.plans IS NULL" in insert
    assert "workspaces" not in insert


# --- #2800: plans --------------------------------------------------------------

_KEYS = "free, basic, pro, promax"


def test_plans_come_back_in_tier_order_as_plain_strings() -> None:
    plans = svc.normalize_notice_plans(["promax", "pro"])
    assert plans == ["pro", "promax"]
    assert all(type(p) is str for p in plans)
    assert svc.normalize_notice_plans(["basic"]) == ["basic"]


def test_all_four_tiers_stay_a_list() -> None:
    assert svc.normalize_notice_plans(["promax", "free", "pro", "basic"]) == [
        "free",
        "basic",
        "pro",
        "promax",
    ]


def test_normalize_is_exported() -> None:
    assert "normalize_notice_plans" in svc.__all__


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param([], id="empty"),
        pytest.param(["enterprise"], id="unknown"),
        pytest.param(["Pro"], id="miscased"),
        pytest.param([" pro"], id="padded"),
        pytest.param(["pro "], id="trailing_space"),
        pytest.param(["pro", "pro"], id="repeated"),
        pytest.param(["pro", 1], id="not_a_string"),
        pytest.param([None], id="none_entry"),
    ],
)
def test_refusals_are_value_errors_that_name_the_keys_not_the_input(plans) -> None:
    with pytest.raises(ValueError) as caught:
        svc.normalize_notice_plans(plans)
    assert type(caught.value) is ValueError
    message = str(caught.value)
    assert _KEYS in message
    assert "enterprise" not in message and "Pro" not in message
    assert repr(plans) not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param([], id="empty"),
        pytest.param(["Pro"], id="miscased"),
        pytest.param(["pro", "pro"], id="repeated"),
    ],
)
async def test_create_notice_refuses_plans_itself_before_any_statement(plans) -> None:
    """``create_notice`` refuses a bad plan list on its own, not only behind
    the route's request model (a direct caller skips that), and before the
    create lock or any query. ``[]`` is refused, never read as "everyone"
    (``None`` is the only way to target every plan)."""
    db = _NoDb()
    with pytest.raises(ValueError) as caught:
        await svc.create_notice(
            db,  # type: ignore[arg-type]
            raw_text="Maintenance on Saturday.",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            starts_at=None,
            plans=plans,
            actor=svc.NoticeActor(email="admin@example.com", user_id="u1"),
        )
    # An AttributeError from the session stand-in is not a refusal.
    assert type(caught.value) is ValueError
    assert db.used == []


@pytest.mark.asyncio
async def test_create_notice_refuses_text_before_any_statement() -> None:
    """The text refusal holds the same order: nothing is locked or read."""
    db = _NoDb()
    with pytest.raises(svc.McpNoticeTextRejectedError):
        await svc.create_notice(
            db,  # type: ignore[arg-type]
            raw_text="   ",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            starts_at=None,
            plans=["pro"],
            actor=svc.NoticeActor(email="admin@example.com", user_id="u1"),
        )
    assert db.used == []


def test_the_f44_downgrade_takes_the_create_lock() -> None:
    """The f44 downgrade serialises with ``create_notice`` through the same
    advisory lock; a rename of one key without the other would silently
    break the downgrade guard."""
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "f44_2800_mcp_notice_plans.py"
    )
    spec = importlib.util.spec_from_file_location("_f44_2800_lock_pin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.CREATE_LOCK_SQL == svc._CREATE_LOCK_SQL.text
