"""Lane resolution for Memory Analysis (#1569).

Pins the acceptance bullets: BYOK enabled + key → strict BYOK lane; no key +
managed lane configured + entitled plan → managed lane (``paid_by='platform'``,
``platform_only``); managed lane configured but plan lacks ``managed_llm`` →
VAL-001; nothing configured → the historical refusal naming both routes.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from config.settings import Settings
from services.analysis.llm_caller import OPENAI_FALLBACK_CHAIN
from services.analysis.llm_lane import (
    byok_lane,
    lane_for_run,
    managed_lane,
    pinned_pricing_id,
    preview_pricing_target,
    resolve_analysis_lane,
    try_resolve_analysis_lane,
)
from utils.exceptions import ValidationError


def _db(*scalars):
    """AsyncMock session whose successive ``execute`` calls yield ``scalars``."""
    db = AsyncMock()
    results = []
    for value in scalars:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=value)
        results.append(result)
    db.execute = AsyncMock(side_effect=results)
    return db


def _settings(monkeypatch, **overrides) -> Settings:
    monkeypatch.setenv("SELF_HOSTED_BASE_URL", "http://vllm:8000")
    return Settings(_env_file=None, **overrides)


_MANAGED = {"managed_llm_provider": "self_hosted", "managed_llm_model": "qwen3:8b"}


class TestResolveAnalysisLane:
    @pytest.mark.asyncio
    async def test_byok_key_wins_and_costs_one_query(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, **_MANAGED)  # managed configured too
        db = _db(uuid4())  # key row exists
        lane = await resolve_analysis_lane(db, workspace_id=uuid4(), settings=settings)
        assert lane == byok_lane()
        assert (lane.kind, lane.provider, lane.paid_by) == ("byok", "openai", "byok")
        assert lane.models == OPENAI_FALLBACK_CHAIN
        assert lane.platform_only is False
        db.execute.assert_awaited_once()  # no plan lookup on the BYOK path

    @pytest.mark.asyncio
    async def test_no_key_entitled_plan_gets_managed_lane(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, **_MANAGED)
        db = _db(None, "pro")  # no key; workspaces.plan_name = pro
        lane = await resolve_analysis_lane(
            db, workspace_id=str(uuid4()), context_id=str(uuid4()), settings=settings
        )
        assert lane == managed_lane("self_hosted", "qwen3:8b")
        assert (lane.paid_by, lane.platform_only, lane.models) == (
            "platform",
            True,
            ("qwen3:8b",),
        )
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_byok_disabled_skips_key_lookup_entirely(self, monkeypatch) -> None:
        """ENABLE_BYOK=false: a stored key would be a leftover — never consulted."""
        settings = _settings(monkeypatch, enable_byok=False, **_MANAGED)
        db = _db("promax")  # the only query is the plan lookup
        lane = await resolve_analysis_lane(
            db, workspace_id=uuid4(), plan_name=None, settings=settings
        )
        assert lane.kind == "managed"
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_caller_supplied_plan_name_saves_the_query(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, enable_byok=False, **_MANAGED)
        db = _db()
        lane = await resolve_analysis_lane(
            db, workspace_id=uuid4(), plan_name="pro", settings=settings
        )
        assert lane.kind == "managed"
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plan_without_managed_llm_is_refused(self, monkeypatch) -> None:
        settings = _settings(monkeypatch, **_MANAGED)
        db = _db(None, "basic")
        with pytest.raises(ValidationError) as excinfo:
            await resolve_analysis_lane(db, workspace_id=uuid4(), settings=settings)
        err = excinfo.value
        assert err.status_code == 422
        assert err.details.get("field") == "byok"
        assert "managed_llm" in str(err) and "basic" in str(err)

    @pytest.mark.asyncio
    async def test_nothing_configured_names_both_routes(self, monkeypatch) -> None:
        settings = _settings(monkeypatch)  # no managed lane
        db = _db(None)
        with pytest.raises(ValidationError) as excinfo:
            await resolve_analysis_lane(db, workspace_id=uuid4(), settings=settings)
        message = str(excinfo.value)
        assert "OpenAI API key not configured" in message
        assert "External Keys" in message
        assert "MANAGED_LLM_PROVIDER" in message
        # No managed lane → the plan is never looked up.
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_override_can_grant_managed_llm_to_free(self, monkeypatch) -> None:
        """PLAN_<KEY>_FEATURES (#1559) decides entitlement, not the tier name."""
        from config import plan_tiers

        free = plan_tiers.PLAN_TIERS["free"]
        monkeypatch.setattr(
            plan_tiers,
            "PLAN_TIERS",
            {
                **plan_tiers.PLAN_TIERS,
                "free": dataclasses.replace(free, features=free.features | {"managed_llm"}),
            },
        )
        settings = _settings(monkeypatch, enable_byok=False, **_MANAGED)
        lane = await resolve_analysis_lane(
            _db(), workspace_id=uuid4(), plan_name="free", settings=settings
        )
        assert lane.kind == "managed"


class TestHelpers:
    @pytest.mark.asyncio
    async def test_try_resolve_returns_none_on_refusal(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "services.analysis.llm_lane.get_settings", lambda: _settings(monkeypatch)
        )
        assert await try_resolve_analysis_lane(_db(None), workspace_id=uuid4()) is None

    @pytest.mark.asyncio
    async def test_preview_pricing_target_follows_lane_or_default(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "services.analysis.llm_lane.get_settings",
            lambda: _settings(monkeypatch, enable_byok=False, **_MANAGED),
        )
        assert await preview_pricing_target(_db("pro"), workspace_id=uuid4(), context_id=None) == (
            "self_hosted",
            "qwen3:8b",
        )
        assert await preview_pricing_target(
            _db("basic"), workspace_id=uuid4(), context_id=None
        ) == ("openai", "gpt-5-nano")

    def test_pinned_pricing_id_passes_through_except_on_managed_lane(self) -> None:
        """A pinned ``llm_pricing`` row is honoured on BYOK and on the
        preview's no-lane default; the managed lane refuses it (the snapshot
        must name the model that actually runs)."""
        assert pinned_pricing_id(byok_lane(), 42) == 42
        assert pinned_pricing_id(None, 42) == 42
        assert pinned_pricing_id(managed_lane("self_hosted", "qwen3:8b"), None) is None
        with pytest.raises(ValidationError) as exc_info:
            pinned_pricing_id(managed_lane("self_hosted", "qwen3:8b"), 42)
        assert exc_info.value.details["field"] == "model_id"
        assert "self_hosted/qwen3:8b" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_preview_pricing_target_refuses_pinned_id_on_managed_lane(
        self, monkeypatch
    ) -> None:
        """``/preview`` applies the same rule as ``start()``: a pinned row on
        the managed lane is a 422, while the no-lane default still takes it."""
        monkeypatch.setattr(
            "services.analysis.llm_lane.get_settings",
            lambda: _settings(monkeypatch, enable_byok=False, **_MANAGED),
        )
        with pytest.raises(ValidationError):
            await preview_pricing_target(
                _db("pro"), workspace_id=uuid4(), context_id=None, model_id=42
            )
        assert await preview_pricing_target(
            _db("basic"), workspace_id=uuid4(), context_id=None, model_id=42
        ) == ("openai", "gpt-5-nano")

    def test_lane_for_run_rebuilds_from_row(self) -> None:
        assert lane_for_run(paid_by="platform", provider="anthropic", model="claude-x") == (
            managed_lane("anthropic", "claude-x")
        )
        assert lane_for_run(paid_by="byok", provider="openai", model="gpt-5-nano") == byok_lane()
        # Pre-e81 rows: NULL lane columns were all BYOK.
        assert lane_for_run(paid_by="byok", provider=None, model=None) == byok_lane()
