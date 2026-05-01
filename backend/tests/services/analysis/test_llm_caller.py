"""Tests for the analysis LLM caller adapter (Stage [G]).

Pins three Phase-4 design contracts:

1. ``test_fallback_stays_within_openai`` — when the primary
   gpt-5-nano fails, the adapter retries gpt-5.5 and NEVER advances
   to a non-OpenAI provider. This is the AC-pinned cross-provider
   prohibition.

2. ``test_502_wrapping_after_chain_exhausted`` — when both models
   in the fallback chain fail, the adapter raises
   ``AnalysisLLMUpstreamError`` (status_code=502 with
   ``upstream_provider_error=True`` detail) — NOT the bare
   ``LLMServiceError`` which would 500.

3. ``test_filter_hallucinated_ids`` — the frozenset guard returns
   only members of the requested set, in input order. Mirrors
   ``services/sleep/edge_discovery.py:818`` pattern.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.analysis.llm_caller import (
    OPENAI_FALLBACK_CHAIN,
    AnalysisLLMUpstreamError,
    call_with_fallback,
    filter_hallucinated_ids,
)
from services.llm_service import LLMResponse, LLMServiceError


def _ok_response(model: str) -> LLMResponse:
    """Build a minimal LLMResponse for happy-path mocks."""
    return LLMResponse(
        parsed={"label": "ok", "description": "ok desc", "label_confidence": 0.9},
        total_tokens=120,
        input_tokens=80,
        output_tokens=40,
        cached_input_tokens=0,
        provider="openai",
        model=model,
    )


@pytest.mark.asyncio
async def test_chain_constants_are_openai_only() -> None:
    """The default fallback chain stays within OpenAI by construction.

    Pin the constant so a future code-search like ``grep gemini``
    inside this file would highlight any drift.
    """
    assert OPENAI_FALLBACK_CHAIN == ("gpt-5-nano", "gpt-5.5")
    # Both models are OpenAI per c03_471_seed_pricing.
    for model in OPENAI_FALLBACK_CHAIN:
        assert model.startswith("gpt-")


@pytest.mark.asyncio
async def test_primary_succeeds_no_fallback_attempted() -> None:
    """Happy path: primary returns OK, fallback is never called."""
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(return_value=_ok_response("gpt-5-nano"))

    result = await call_with_fallback(
        llm_service,
        user_id="u1",
        workspace_id="w1",
        context_id=None,
        system_prompt="sys",
        prompt="p",
    )

    assert result.response.model == "gpt-5-nano"
    assert llm_service.complete_json.call_count == 1


@pytest.mark.asyncio
async def test_fallback_stays_within_openai() -> None:
    """When primary fails, the adapter retries the next OpenAI model.

    The mock complete_json sees the calls in order; we assert the
    second call's ``model`` kwarg is from ``OPENAI_FALLBACK_CHAIN``,
    NOT a non-OpenAI provider. The test fails if a future contributor
    edits the chain to include a cross-provider fallback.
    """
    primary_error = LLMServiceError("primary unavailable")
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(side_effect=[primary_error, _ok_response("gpt-5.5")])

    result = await call_with_fallback(
        llm_service,
        user_id="u1",
        workspace_id="w1",
        context_id=None,
        system_prompt="sys",
        prompt="p",
    )

    assert result.response.model == "gpt-5.5"
    assert llm_service.complete_json.call_count == 2
    # Both calls must specify provider='openai'. A non-openai value
    # in either call would mean cross-provider fallback was attempted.
    for call in llm_service.complete_json.call_args_list:
        assert call.kwargs["provider"] == "openai", (
            f"Cross-provider fallback detected: {call.kwargs}"
        )


@pytest.mark.asyncio
async def test_502_wrapping_after_chain_exhausted() -> None:
    """All models in the fallback chain fail → AnalysisLLMUpstreamError(502)."""
    llm_service = AsyncMock()
    llm_service.complete_json = AsyncMock(
        side_effect=[
            LLMServiceError("nano down"),
            LLMServiceError("5.5 down"),
        ]
    )

    with pytest.raises(AnalysisLLMUpstreamError) as excinfo:
        await call_with_fallback(
            llm_service,
            user_id="u1",
            workspace_id="w1",
            context_id=None,
            system_prompt="sys",
            prompt="p",
        )

    err = excinfo.value
    assert err.status_code == 502
    assert err.error_code == "EXT-ANA-001"
    assert err.details.get("upstream_provider_error") is True
    assert err.details.get("attempted_models") == list(OPENAI_FALLBACK_CHAIN)
    # The exception's message must not include the BYOK key — but
    # since the mock didn't pass one, this just confirms the
    # message is bounded.
    assert "All fallback models exhausted" in str(err)


def test_filter_hallucinated_ids_drops_non_members() -> None:
    """The frozenset guard returns only members, preserving input order."""
    requested = frozenset({"id-1", "id-2", "id-3"})
    candidates = ["id-2", "id-fake", "id-1", "id-also-fake"]

    out = filter_hallucinated_ids(candidates, requested)

    # Order preserved; only members kept.
    assert out == ["id-2", "id-1"]


def test_filter_hallucinated_ids_empty_input() -> None:
    """Empty candidate list returns empty list."""
    requested = frozenset({"id-1"})
    assert filter_hallucinated_ids([], requested) == []


def test_filter_hallucinated_ids_all_hallucinated() -> None:
    """All candidates outside the requested set returns empty list."""
    requested = frozenset({"id-1"})
    assert filter_hallucinated_ids(["id-evil-1", "id-evil-2"], requested) == []
