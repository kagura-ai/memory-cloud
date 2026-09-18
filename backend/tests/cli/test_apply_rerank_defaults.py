"""Integration tests for ``cli/apply_rerank_defaults`` (#1572).

Needs a live Postgres (``TEST_DATABASE_URL``, ``*_test`` suffixed); the
``db_session`` fixture skips otherwise. Pins the acceptance bullet: the ops
command converts code-default rows, leaves explicit ones alone, writes nothing
in dry-run, and is a no-op when re-run after ``--apply``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from models.auth import Context, Workspace
from models.config import ContextSearchConfig

# The CLI bootstraps sys.path itself; import it the same way create_admin's test does.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli.apply_rerank_defaults import (  # noqa: E402
    apply_rerank_defaults,
    is_code_default,
)

_RERANK_ENV = [
    "RERANK_BASE_URL",
    "RERANK_MODEL",
    "SELF_HOSTED_BASE_URL",
    "DEFAULT_RERANKER_PROVIDER",
    "DEFAULT_USE_RERANK",
    "DEFAULT_RERANKER_MODEL",
]


@pytest.fixture
def clean_env(monkeypatch):
    for key in _RERANK_ENV:
        monkeypatch.delenv(key, raising=False)


def _self_hosted_settings() -> Settings:
    """A deployment that made the keyless vLLM reranker its default."""
    return Settings(
        _env_file=None,
        rerank_base_url="http://gpu:8002",
        rerank_model="qwen3-reranker-0.6b",
        default_reranker_provider="self_hosted",
        default_use_rerank=True,
    )


def _workspace(owner: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="basic",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _context(ws: Workspace, owner: str) -> Context:
    return Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=True,
    )


def _config(ctx: Context, *, use_rerank: bool, provider: str, model: str) -> ContextSearchConfig:
    return ContextSearchConfig(
        context_id=ctx.id,
        use_rerank=use_rerank,
        reranker_provider=provider,
        reranker_model=model,
        embedding_model="text-embedding-3-small",
        embedding_dimensions=512,
    )


@pytest_asyncio.fixture
async def scenario(db_session: AsyncSession):
    """One workspace with four rows (two code-default, two explicit) plus a
    code-default row in ANOTHER workspace, to pin the scope filter."""
    owner = f"owner_{uuid4().hex[:8]}"
    ws = _workspace(owner)
    other_ws = _workspace(owner)

    default_ctx = _context(ws, owner)
    lite_ctx = _context(ws, owner)  # pre-#1572 create_context wrote rerank-2-lite
    explicit_on_ctx = _context(ws, owner)
    explicit_off_ctx = _context(ws, owner)  # off, but a chosen provider
    other_ctx = _context(other_ws, owner)

    # Flush in dependency order — no back-populating relationship() between
    # Context → Workspace (same pattern as test_edge_context_invariant.py).
    db_session.add_all([ws, other_ws])
    await db_session.flush()
    db_session.add_all([default_ctx, lite_ctx, explicit_on_ctx, explicit_off_ctx, other_ctx])
    await db_session.flush()
    db_session.add_all(
        [
            _config(default_ctx, use_rerank=False, provider="voyage", model="rerank-2"),
            _config(lite_ctx, use_rerank=False, provider="voyage", model="rerank-2-lite"),
            _config(
                explicit_on_ctx,
                use_rerank=True,
                provider="cohere",
                model="rerank-multilingual-v3.0",
            ),
            _config(explicit_off_ctx, use_rerank=False, provider="self_hosted", model="custom"),
            _config(other_ctx, use_rerank=False, provider="voyage", model="rerank-2"),
        ]
    )
    await db_session.flush()

    yield {
        "ws": ws,
        "default": default_ctx.id,
        "lite": lite_ctx.id,
        "explicit_on": explicit_on_ctx.id,
        "explicit_off": explicit_off_ctx.id,
        "other": other_ctx.id,
    }

    await db_session.rollback()


async def _row(db: AsyncSession, context_id: UUID) -> ContextSearchConfig:
    result = await db.execute(
        select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
    )
    return result.scalar_one()


def test_is_code_default_matches_only_the_never_chosen_values():
    def row(use_rerank, provider, model):
        return ContextSearchConfig(
            context_id=uuid4(),
            use_rerank=use_rerank,
            reranker_provider=provider,
            reranker_model=model,
        )

    assert is_code_default(row(False, "voyage", "rerank-2"))
    assert is_code_default(row(False, "voyage", "rerank-2-lite"))
    assert not is_code_default(row(True, "voyage", "rerank-2"))
    assert not is_code_default(row(False, "cohere", "rerank-multilingual-v3.0"))
    assert not is_code_default(row(False, "voyage", "rerank-2.5"))
    assert not is_code_default(row(False, "self_hosted", "rerank-2"))


@pytest.mark.asyncio(loop_scope="session")
async def test_plan_converts_code_default_rows_skips_explicit_and_writes_nothing(
    db_session: AsyncSession, scenario, clean_env
):
    result = await apply_rerank_defaults(
        db_session,
        workspace_id=scenario["ws"].id,
        dry_run=True,
        settings=_self_hosted_settings(),
    )

    assert result.dry_run is True
    assert result.target == {
        "use_rerank": True,
        "reranker_provider": "self_hosted",
        "reranker_model": "qwen3-reranker-0.6b",
    }
    verdicts = {line.context_id: line.action for line in result.lines}
    assert verdicts == {
        scenario["default"]: "convert",
        scenario["lite"]: "convert",
        scenario["explicit_on"]: "skip",
        scenario["explicit_off"]: "skip",
    }, "other-workspace row must be out of scope"
    assert result.converted == 2 and result.skipped == 2

    # Dry run: the rows are untouched.
    untouched = await _row(db_session, scenario["default"])
    assert (untouched.use_rerank, untouched.reranker_provider, untouched.reranker_model) == (
        False,
        "voyage",
        "rerank-2",
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_writes_target_and_rerun_is_a_noop(
    db_session: AsyncSession, scenario, clean_env
):
    settings = _self_hosted_settings()

    applied = await apply_rerank_defaults(
        db_session, workspace_id=scenario["ws"].id, dry_run=False, settings=settings
    )
    assert sorted(applied.converted_ids) == sorted([scenario["default"], scenario["lite"]])

    for cid in (scenario["default"], scenario["lite"]):
        row = await _row(db_session, cid)
        assert (row.use_rerank, row.reranker_provider, row.reranker_model) == (
            True,
            "self_hosted",
            "qwen3-reranker-0.6b",
        )
    explicit = await _row(db_session, scenario["explicit_on"])
    assert (explicit.use_rerank, explicit.reranker_provider, explicit.reranker_model) == (
        True,
        "cohere",
        "rerank-multilingual-v3.0",
    )
    other = await _row(db_session, scenario["other"])
    assert other.reranker_provider == "voyage", "other workspace untouched"

    again = await apply_rerank_defaults(
        db_session, workspace_id=scenario["ws"].id, dry_run=False, settings=settings
    )
    assert again.converted == 0
    assert again.scanned == 4


@pytest.mark.asyncio(loop_scope="session")
async def test_all_scope_spans_workspaces(db_session: AsyncSession, scenario, clean_env):
    result = await apply_rerank_defaults(
        db_session, workspace_id=None, dry_run=True, settings=_self_hosted_settings()
    )
    converted = set(result.converted_ids)
    # Membership, not counts: --all also sees rows other tests may have committed.
    assert {scenario["default"], scenario["lite"], scenario["other"]} <= converted
    assert scenario["explicit_on"] not in converted


@pytest.mark.asyncio(loop_scope="session")
async def test_unset_deployment_defaults_only_normalize_the_lite_model(
    db_session: AsyncSession, scenario, clean_env
):
    """Defaults unset → target is the code default (off / voyage / rerank-2):
    the rerank-2 row is already there, the pre-#1572 rerank-2-lite row is
    normalized, explicit rows stay."""
    result = await apply_rerank_defaults(
        db_session,
        workspace_id=scenario["ws"].id,
        dry_run=True,
        settings=Settings(_env_file=None),
    )
    verdicts = {line.context_id: line.action for line in result.lines}
    assert verdicts[scenario["default"]] == "skip"
    assert verdicts[scenario["lite"]] == "convert"
    assert verdicts[scenario["explicit_on"]] == "skip"
    assert verdicts[scenario["explicit_off"]] == "skip"
