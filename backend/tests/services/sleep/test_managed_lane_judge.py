"""Sleep judge on the managed LLM lane (#1569).

- ``judge_platform_only``: True only when the judge pair came from
  MANAGED_LLM_* AND the deployment set RESOLVE_STORED_BYOK_KEYS=false.
- End to end with ``self_hosted``: a real ``LLMService`` + real
  ``SelfHostedProvider`` (HTTP client mocked) — the judge call carries
  ``response_format``, never touches ``external_api_keys``, and a backend
  failure still counts toward the #1183 ``degraded`` grading.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.llm_service import LLMService
from services.sleep.importance_reeval import ImportanceReevalPhase
from services.sleep.judge_lane import judge_platform_only
from services.sleep.reporter import SleepBudget


def _config(*, from_managed: bool, provider: str = "self_hosted", model: str = "qwen3:8b"):
    config = MagicMock()
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    config.sleep_llm_from_managed_lane = from_managed
    return config


class TestJudgePlatformOnly:
    @pytest.mark.parametrize(
        ("from_managed", "resolve_stored", "expected"),
        [
            (True, False, True),  # managed lane + stored keys ruled out
            (True, True, False),  # managed lane but stored keys still honoured
            (False, False, False),  # explicit SLEEP_LLM_* / DB row: BYOK-then-env
            (False, True, False),
        ],
    )
    def test_truth_table(self, monkeypatch, from_managed, resolve_stored, expected) -> None:
        monkeypatch.setattr(
            "services.sleep.judge_lane.get_settings",
            lambda: MagicMock(resolve_stored_byok_keys=resolve_stored),
        )
        assert judge_platform_only(_config(from_managed=from_managed)) is expected


def _settings_stub() -> MagicMock:
    settings = MagicMock()
    settings.resolve_stored_byok_keys = False
    settings.self_hosted_base_url = "http://vllm:8000"
    settings.self_hosted_api_key = ""
    settings.self_hosted_model_aliases = ""
    settings.self_hosted_llm_timeout_seconds = 60.0
    return settings


def _chat_response(content: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    response.usage = MagicMock(prompt_tokens=30, completion_tokens=12, total_tokens=42)
    return response


def _memory() -> MagicMock:
    m = MagicMock()
    m.id = uuid4()
    m.summary = "a memory summary"
    m.type = "note"
    m.importance = 0.5
    m.access_count = 2
    m.scope = "working"
    return m


class TestSelfHostedJudgeEndToEnd:
    @pytest.fixture
    def rig(self, monkeypatch):
        """Real LLMService → real SelfHostedProvider over a fake OpenAI client."""
        settings = _settings_stub()
        # Every settings reader on the path sees the same managed-lane deployment.
        monkeypatch.setattr("services.sleep.judge_lane.get_settings", lambda: settings)
        monkeypatch.setattr("services.byok_resolution.get_settings", lambda: settings)
        monkeypatch.setattr("services.byok_resolution._disabled_logged", True)
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)
        monkeypatch.setattr(
            "services.llm_providers.self_hosted_provider.get_settings", lambda: settings
        )
        monkeypatch.setattr(
            "services.llm_providers.self_hosted_provider.verify_self_hosted_reachable",
            AsyncMock(),
        )
        client = MagicMock()
        client.chat.completions.create = AsyncMock()
        monkeypatch.setattr(
            "services.llm_providers.self_hosted_provider.AsyncOpenAI", lambda **_kw: client
        )

        db = AsyncMock()
        with patch(
            "services.sleep.importance_reeval.update_memory_payload_in_qdrant",
            new_callable=AsyncMock,
        ):
            phase = ImportanceReevalPhase(db, LLMService(db))
        phase._tokens_used = 0
        phase._llm_breakdown = None
        phase._llm_failures = 0
        return phase, client, db

    @pytest.mark.asyncio
    async def test_judge_call_runs_on_self_hosted_with_json_mode(self, rig) -> None:
        phase, client, db = rig
        client.chat.completions.create.return_value = _chat_response(
            '{"scores": [{"label": "A", "importance": 0.8}]}'
        )
        mem = _memory()
        budget = SleepBudget()

        scores = await phase._evaluate_batch(
            [mem], "u1", str(uuid4()), str(uuid4()), budget, _config(from_managed=True)
        )

        assert scores == {mem.id: 0.8}
        assert phase._llm_failures == 0
        assert budget.llm_calls_used == 1
        assert phase._tokens_used == 42
        assert phase._llm_breakdown is not None and phase._llm_breakdown.calls == 1
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "qwen3:8b"
        assert kwargs["response_format"] == {"type": "json_object"}
        # platform_only: the workspace's external_api_keys were never consulted.
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_backend_failure_still_counts_as_judge_failure(self, rig) -> None:
        """#1183 degraded grading is unchanged on the managed lane."""
        phase, client, _db = rig
        client.chat.completions.create.side_effect = RuntimeError("vllm down")

        scores = await phase._evaluate_batch(
            [_memory()], "u1", None, str(uuid4()), SleepBudget(), _config(from_managed=True)
        )

        assert scores == {}
        assert phase._llm_failures == 1
